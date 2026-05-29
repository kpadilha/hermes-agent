"""Configuration management for Hermes Agent: config.yaml / .env loading, saving,
validation, migration, and the ``hermes config`` command."""

import copy
import difflib
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Set

import yaml

from hermes_cli.cli_output import line_input
from hermes_cli.colors import Colors, color
from hermes_cli import managed_scope
from hermes_cli.default_soul import DEFAULT_SOUL_MD, is_legacy_template_soul
from hermes_cli.secret_prompt import masked_secret_prompt
# Re-export from hermes_constants — canonical definition lives there.
from hermes_constants import get_hermes_home, get_process_hermes_home  # noqa: F401
from utils import atomic_replace, atomic_yaml_write, fast_safe_load

logger = logging.getLogger(__name__)

# (config_path, mtime_ns, size) tuples already warned about, so concurrent CLI/gateway
# loads of a broken config.yaml don't spam stderr. A changed file (new mtime) warns again.
_CONFIG_PARSE_WARNED: set = set()

# path -> (mtime_ns, size, error message) of active parse failures. Written by
# _warn_config_parse_failure() (the single funnel for every load-path parse failure) and
# probed by get_active_config_parse_failure() so provider auto-resolution can refuse to
# adopt a paid provider from env keys while the user's REAL config is unreadable.
_CONFIG_PARSE_FAILURES: dict = {}


class InvalidUserConfigError(RuntimeError):
    """Raised when a run that cannot repair config finds invalid user YAML."""


def _backup_corrupt_config(config_path: Path) -> Optional[Path]:
    """Copy an unparseable ``config.yaml`` to a timestamped ``.corrupt.*.bak``; None on skip/failure.
    Symlinks are not followed (never clobber whatever a malicious symlink points at). A sibling
    backup of the same size means this corruption was already snapshotted — skip to avoid churn.

    Returns the backup path on success, else ``None``. See #21541.
    """
    try:
        if config_path.is_symlink():
            return None
        st = config_path.stat()
        if st.st_size == 0:
            return None
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = config_path.with_name(f"{config_path.name}.corrupt.{ts}.bak")
        for existing in config_path.parent.glob(f"{config_path.name}.corrupt.*.bak"):
            try:
                if existing.stat().st_size == st.st_size:
                    return None
            except OSError:
                continue
        if backup_path.exists():
            return None
        shutil.copy2(config_path, backup_path)
        return backup_path
    except Exception:
        return None


_PARSE_FAILURE_FALLBACK_MSG = {
    "last-known-good": (
        "Keeping the previously loaded config for this process — "
        "edits to config.yaml are being IGNORED until the YAML is fixed."),
    "refuse-write": (
        "REFUSING to write config.yaml so the existing file is preserved. "
        "Fix the YAML (hermes config edit) and retry.")}
_PARSE_FAILURE_DEFAULTS_MSG = (
    "Falling back to default config — every user override (auxiliary providers, fallback chain, "
    "model settings) is being IGNORED. Fix the YAML and restart.")


def _warn_config_parse_failure(
    config_path: Path, exc: Exception, *, fallback: str = "defaults") -> None:
    """Surface a config.yaml parse failure to log and stderr (once per file signature).
    Silent fallback to ``DEFAULT_CONFIG`` drops every user override, so this must be loud.

    ``fallback`` selects the message wording: ``"defaults"`` (fresh process, nothing else to serve) or
    ``"last-known-good"`` (in-process retention of the previously loaded config — see the codex#31188 port
    in ``_load_config_impl``).
    """
    try:
        st = config_path.stat()
        key = (str(config_path), st.st_mtime_ns, st.st_size)
        _CONFIG_PARSE_FAILURES[str(config_path)] = (st.st_mtime_ns, st.st_size, str(exc))
    except OSError:
        key = (str(config_path), 0, 0)
    if key in _CONFIG_PARSE_WARNED:
        return
    _CONFIG_PARSE_WARNED.add(key)
    backup_path = _backup_corrupt_config(config_path)
    msg = f"Failed to parse {config_path}: {exc}. " + _PARSE_FAILURE_FALLBACK_MSG.get(
        fallback, _PARSE_FAILURE_DEFAULTS_MSG)
    if backup_path is not None:
        msg += f" A copy of the corrupted file was saved to {backup_path}."
    logger.warning(msg)
    try:
        sys.stderr.write(f"⚠️  hermes config: {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def get_active_config_parse_failure() -> Optional[str]:
    """Return the recorded parse error while the ACTIVE config.yaml is still byte-identical
    (mtime_ns + size) to the file that failed to parse; else None."""
    try:
        path = get_config_path()
        mtime_ns, size, err = _CONFIG_PARSE_FAILURES[str(path)]
        st = path.stat()
        return err if (st.st_mtime_ns, st.st_size) == (mtime_ns, size) else None
    except Exception:
        return None


_IS_WINDOWS = platform.system() == "Windows"
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Env var names that influence how the next subprocess executes — never writable through
# ``save_env_value``: dynamic loader (LD_*/DYLD_*: attacker code loads before main()),
# interpreter init (PYTHON*, NODE_*: Hermes restarts through them), PATH (fix tool lookup
# with absolute paths instead), git rewrites (fire on every plugin install/update),
# implicitly-invoked commands (BROWSER/EDITOR/VISUAL/PAGER = RCE on next $EDITOR), SHELL,
# and Hermes runtime-location / security-policy flags (config.yaml is the supported surface).
#
# ``HERMES_*`` overall is NOT blocked — many integration credentials use that prefix
# (HERMES_LANGFUSE_PUBLIC_KEY, HERMES_SPOTIFY_CLIENT_ID, ...). The denylist is name-by-name so
# it cannot break provider setup wizards. Enforced on *write* only: pre-existing/out-of-band
# ``.env`` values keep working; the dashboard's writable surface just cannot escalate.
_ENV_VAR_NAME_DENYLIST: frozenset[str] = frozenset({
    # Loader / linker
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FALLBACK_FRAMEWORK_PATH",
    # Python / Node
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "PYTHONEXECUTABLE", "PYTHONNOUSERSITE", "NODE_OPTIONS", "NODE_PATH",
    # General / git
    "PATH", "SHELL", "BROWSER", "EDITOR", "VISUAL", "PAGER",
    "GIT_SSH_COMMAND", "GIT_EXEC_PATH", "GIT_SHELL",
    # Hermes runtime location
    "HERMES_HOME", "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV",
    "HERMES_CONFIG_PATH", "HERMES_ENV_PATH",
    # MCP catalog trust root; package-manager wrappers may still set it in the process env.
    "HERMES_OPTIONAL_MCPS",
    # Local ACP subprocess selection (executable/argv authority).
    "HERMES_COPILOT_ACP_COMMAND", "HERMES_COPILOT_ACP_ARGS",
    # Security policy / approval-routing context — set via their dedicated controls only.
    "HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS", "HERMES_REDACT_SECRETS",
    "HERMES_INTERACTIVE", "HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION",
    "HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION",
    "HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM"})


def _env_var_policy_name(key: str, *, is_windows: Optional[bool] = None) -> str:
    """Name used for env policy comparisons: Windows env names are case-insensitive, POSIX not.
    The override keeps both semantics testable on any host."""
    windows = _IS_WINDOWS if is_windows is None else is_windows
    return key.upper() if windows else key


def validate_env_var_name_for_write(key: str) -> None:
    """Validate an env name before a generic persistence write (exposed for batch callers)."""
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    if _env_var_policy_name(key) in _ENV_VAR_NAME_DENYLIST:
        raise ValueError(
            f"Environment variable {key!r} is on the writer denylist. "
            "Names that influence subprocess execution (LD_PRELOAD, PYTHONPATH, PATH, EDITOR, ...) "
            "or Hermes runtime location and security policy (HERMES_HOME, HERMES_YOLO_MODE, ...) "
            "cannot be persisted via the env writer. If you really need this, edit ~/.hermes/.env "
            "directly.")


# Serializes all config read/write paths and guards the module-level caches below. libyaml's
# C extension is not thread-safe for concurrent safe_load() on one file, and tool threads
# (approval, browser, setup flows) load/save config concurrently during long agent runs.
# RLock because save_config internally calls read_raw_config.
_CONFIG_LOCK = threading.RLock()
# path -> last successfully loaded (expanded) config; served after a parse failure so a
# mid-edit broken YAML never silently drops user overrides (e.g. approvals.deny rules).
_LAST_EXPANDED_CONFIG_BY_PATH: Dict[str, Any] = {}
# path -> (user_mtime_ns, user_size, managed_mtime_ns, managed_size, merged, env_ref_snapshot).
# load_config() returns a deepcopy of the cached value while the signature matches (skips
# safe_load + merge + normalize + expand, ~13 ms). Writers use atomic_yaml_write (fresh inode
# -> new mtime_ns) so no explicit invalidation is needed. The managed-file signature is folded
# in so editing the managed-scope config.yaml invalidates, and the env snapshot invalidates
# when a referenced ${VAR} changes value (late .env load, in-process rotation).
# (path, mtime_ns, size) -> cached expanded config dict. load_config() returns a deepcopy of the cached
# value when the file hasn't changed since the last load, skipping yaml.safe_load + _deep_merge +
# _normalize_* + _expand_env_vars (~13 ms/call). save_config() + migrate_config() write via
# atomic_yaml_write which produces a fresh inode, so stat() sees a new mtime_ns and the next load
# repopulates automatically — no explicit invalidation hook. See #58514.
_LOAD_CONFIG_CACHE: Dict[str, Tuple[int, int, int, int, Dict[str, Any], Dict[str, Optional[str]]]] = {}
# path -> (mtime_ns, size, raw yaml dict) for read_raw_config() (no defaults merged in).
_RAW_CONFIG_CACHE: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}

# Env var names written to .env that aren't in OPTIONAL_ENV_VARS (managed by setup/provider
# flows directly). Also the set reload_env() may remove from os.environ.
_EXTRA_ENV_KEYS = frozenset({
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    "DISCORD_HOME_CHANNEL", "DISCORD_HOME_CHANNEL_NAME",
    "TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL_NAME",
    "SLACK_HOME_CHANNEL", "SLACK_HOME_CHANNEL_NAME",
    "SIGNAL_ACCOUNT", "SIGNAL_HTTP_URL", "SIGNAL_ALLOWED_USERS", "SIGNAL_GROUP_ALLOWED_USERS",
    "SIGNAL_HOME_CHANNEL", "SIGNAL_HOME_CHANNEL_NAME", "SMS_HOME_CHANNEL", "SMS_HOME_CHANNEL_NAME",
    "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "DINGTALK_HOME_CHANNEL", "DINGTALK_HOME_CHANNEL_NAME",
    "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_ENCRYPT_KEY", "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_HOME_CHANNEL", "FEISHU_HOME_CHANNEL_NAME", "YUANBAO_HOME_CHANNEL", "YUANBAO_HOME_CHANNEL_NAME",
    "WECOM_BOT_ID", "WECOM_SECRET", "WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET",
    "WECOM_CALLBACK_AGENT_ID", "WECOM_CALLBACK_TOKEN", "WECOM_CALLBACK_ENCODING_AES_KEY",
    "WECOM_CALLBACK_HOST", "WECOM_CALLBACK_PORT", "WECOM_HOME_CHANNEL", "WECOM_HOME_CHANNEL_NAME",
    "WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN", "WEIXIN_BASE_URL", "WEIXIN_CDN_BASE_URL",
    "WEIXIN_HOME_CHANNEL", "WEIXIN_HOME_CHANNEL_NAME", "WEIXIN_DM_POLICY", "WEIXIN_GROUP_POLICY",
    "WEIXIN_ALLOWED_USERS", "WEIXIN_GROUP_ALLOWED_USERS", "WEIXIN_ALLOW_ALL_USERS",
    "BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD", "BLUEBUBBLES_HOME_CHANNEL", "BLUEBUBBLES_HOME_CHANNEL_NAME",
    "QQ_APP_ID", "QQ_CLIENT_SECRET", "QQBOT_HOME_CHANNEL", "QQBOT_HOME_CHANNEL_NAME",
    "QQ_HOME_CHANNEL", "QQ_HOME_CHANNEL_NAME",  # legacy aliases (pre-rename, still read for back-compat)
    "QQ_ALLOWED_USERS", "QQ_GROUP_ALLOWED_USERS", "QQ_ALLOW_ALL_USERS", "QQ_MARKDOWN_SUPPORT",
    "QQ_STT_API_KEY", "QQ_STT_BASE_URL", "QQ_STT_MODEL",
    "IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS", "IRC_SERVER_PASSWORD",
    "IRC_NICKSERV_PASSWORD", "TERMINAL_ENV", "TERMINAL_SSH_KEY", "TERMINAL_SSH_PORT",
    # Deprecated (replaced by display.tool_progress) but STILL READ by the gateway as a
    # back-compat fallback. The boolean HERMES_TOOL_PROGRESS variant is unsupported (its only
    # consumer, the v3->4 migration, is below the v12 support floor); doctor flags it as ignored.
    "HERMES_TOOL_PROGRESS_MODE",
    "WHATSAPP_MODE", "WHATSAPP_ENABLED",
    "MATTERMOST_HOME_CHANNEL", "MATTERMOST_HOME_CHANNEL_NAME", "MATTERMOST_REPLY_MODE",
    "MATRIX_PASSWORD", "MATRIX_ENCRYPTION", "MATRIX_DEVICE_ID", "MATRIX_HOME_ROOM",
    "MATRIX_REQUIRE_MENTION", "MATRIX_FREE_RESPONSE_ROOMS", "MATRIX_AUTO_THREAD", "MATRIX_DM_AUTO_THREAD",
    "MATRIX_RECOVERY_KEY",
    # Langfuse observability plugin tuning keys + standard SDK vars (activation is via
    # plugins.enabled; credentials gate the plugin at runtime).
    "HERMES_LANGFUSE_ENV", "HERMES_LANGFUSE_RELEASE", "HERMES_LANGFUSE_SAMPLE_RATE",
    "HERMES_LANGFUSE_MAX_CHARS", "HERMES_LANGFUSE_CAPTURE", "HERMES_LANGFUSE_DEBUG",
    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
    # ACP (Agent Client Protocol) keys — profile-isolable so profiles can use different backends.
    "HERMES_ACP_AUTH_METHOD", "HERMES_ACP_AUTO_APPROVE", "HERMES_COPILOT_ACP_COMMAND",
    "HERMES_COPILOT_ACP_ARGS", "COPILOT_CLI_PATH", "COPILOT_ACP_BASE_URL"})


# ---- Managed mode (NixOS declarative config) ----

_MANAGED_TRUE_VALUES = ("true", "1", "yes")
_NIX_MANAGED_SYSTEMS = {"nixos", "home-manager"}
# Only the NixOS module ever wrote a bare "true" or an empty marker.
_LEGACY_MANAGED_SYSTEM = "nixos"
# Nix store root; identifies `nix run` / `nix profile install` installs (which don't set
# HERMES_MANAGED). Module-level so tests can patch it without touching /nix/store.
_NIX_STORE = Path("/nix/store")
# Homebrew is no longer a supported distribution: these markers fall through to git/unknown
# detection instead of blocking config writes.
_IGNORED_MANAGED_VALUES = frozenset({"brew", "homebrew"})


def get_managed_system() -> Optional[str]:
    """Return the package manager owning this install, if any.
    Signals: HERMES_MANAGED env var (systemd service) or a ``.managed`` marker file in
    HERMES_HOME (NixOS activation script — interactive shells don't see the service env)."""
    marker = os.getenv("HERMES_MANAGED", "").strip().lower() or None
    managed_marker = get_hermes_home() / ".managed"
    if marker is None and managed_marker.exists():
        try:
            marker = managed_marker.read_text(encoding="utf-8", errors="replace").strip().lower()
        except OSError:
            marker = ""
    if marker is None or marker in _IGNORED_MANAGED_VALUES:
        return None
    if marker == "" or marker in _MANAGED_TRUE_VALUES:
        return _LEGACY_MANAGED_SYSTEM
    return marker


def is_managed() -> bool:
    """Check if Hermes is running in package-manager-managed mode."""
    return get_managed_system() is not None


# Nix installs arrive by several routes (nix run, nix profile, system flake, home-manager) and
# the running process cannot tell which, so the text names the routes instead of one command.
_NIX_UPDATE_MSG = (
    "Update Hermes through the Nix source that installed it "
    "(e.g. nix profile upgrade, or update your flake input and rebuild with nixos-rebuild or home-manager switch)"
)


def get_managed_update_command() -> Optional[str]:
    """Return the preferred upgrade command for a managed install."""
    return _NIX_UPDATE_MSG if get_managed_system() in _NIX_MANAGED_SYSTEMS else None


# "apt" is the Termux APT distribution identifier, not a generic Debian/Ubuntu signal; another
# APT distribution needs its own method. "home-manager" is listed because the managed marker can
# return it and a stamp must name every method this function returns.
_SUPPORTED_INSTALL_METHODS = frozenset({"apt", "docker", "nix", "nixos", "home-manager", "git", "unknown"})


def _install_method_stamp(path: Path) -> Optional[str]:
    try:
        method = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return method if method in _SUPPORTED_INSTALL_METHODS else None


def detect_install_method(project_root: Optional[Path] = None) -> str:
    """Detect how Hermes was installed: apt/docker/nix/nixos/home-manager/git/unknown.
    Order: code-scoped ``<install tree>/.install_method`` stamp (authoritative) -> legacy
    ``$HERMES_HOME/.install_method`` -> managed marker -> /nix/store path -> .git dir -> unknown.
    The stamp lives next to the code because HERMES_HOME is shared data: a container and a host
    install can bind-mount the same home, so a home-scoped ``docker`` stamp would make the host
    ``hermes update`` refuse to run. A legacy ``docker`` value is therefore ignored unless we are
    really inside a container, and being in a container alone never implies 'docker'.

    The supported installs self-identify via the code-scoped stamp: - the curl installer
    (scripts/install.sh, the README/website install command) git-clones the repo and stamps ``git`` next to
    the code; - the published ``nousresearch/hermes-agent`` image bakes a ``docker`` stamp into
    ``/opt/hermes`` at build time. An unsupported manual install dropped into a container (no stamp) falls
    through to the ``.git`` checks and behaves like any off-path install. See issue #34397.
    """
    # The stamp is a property of the running code tree (parent of hermes_cli/), NOT of $HERMES_HOME,
    # so it survives two installs sharing a home.
    root = project_root if project_root is not None else get_project_root()
    method = _install_method_stamp(root / ".install_method")
    if method:
        return method

    method = _install_method_stamp(get_hermes_home() / ".install_method")
    if method and not (method == "docker" and not _running_in_container()):
        return method

    managed = get_managed_system()
    if managed:
        return managed.lower().replace(" ", "-")

    # Code under /nix/store/ is the hallmark of a nix-built install.
    try:
        resolved = root.resolve()
        if resolved != _NIX_STORE and _NIX_STORE in resolved.parents:
            return "nix"
    except OSError:
        pass

    # A .git directory, or a ``gitdir:`` pointer file for worktrees.
    git_path = root / ".git"
    try:
        if git_path.is_dir() or git_path.read_text(encoding="utf-8").strip().startswith("gitdir:"):
            return "git"
    except OSError:
        pass
    return "unknown"


def _running_in_container() -> bool:
    """Import-safe wrapper around ``hermes_constants.is_container``."""
    try:
        from hermes_constants import is_container

        return is_container()
    except Exception:
        return False


def is_nix_install_method(method: str) -> bool:
    """True for every install method Nix owns ("nix", "nixos", "home-manager")."""
    return method == "nix" or method in _NIX_MANAGED_SYSTEMS


_UPDATE_COMMAND_BY_METHOD = {
    "docker": "docker pull nousresearch/hermes-agent:latest",
    "apt": "pkg upgrade hermes-agent",  # "apt" == Termux APT by contract; uses Termux's `pkg`.
}


def recommended_update_command_for_method(method: str) -> str:
    """Return the update command or guidance for a given install method."""
    if is_nix_install_method(method):
        return _NIX_UPDATE_MSG
    return _UPDATE_COMMAND_BY_METHOD.get(method, "hermes update")


def recommended_update_command() -> str:
    """Return the best update command for the current installation.
    Managed state wins over the code-scoped stamp: a managed install can carry a stale stamp
    naming an update path the managed guard refuses."""
    return get_managed_update_command() or recommended_update_command_for_method(
        detect_install_method(get_project_root()))


# Shared by ``cmd_update`` and ``_cmd_update_check`` (hermes_cli/main.py) so the wording never
# forks. The published image excludes ``.git``, so the git update path can never succeed there
# and the generic "reinstall via install.sh" fallback would install a NEW host-side Hermes.
_DOCKER_UPDATE_MESSAGE = """\
✗ ``hermes update`` doesn't apply inside the Docker container.

Hermes Agent runs as a published image (nousresearch/hermes-agent), not a
git checkout — the container has no working tree to pull into.  Update by
pulling a fresh image and restarting your container instead:

  docker pull nousresearch/hermes-agent:latest
  # then restart whatever started the container, e.g.:
  docker compose up -d --force-recreate hermes-agent
  # or, for ad-hoc runs, exit the current container and `docker run` again

Verify the new version after restart:
  docker run --rm nousresearch/hermes-agent:latest --version

Notes:
  • If you pinned a specific tag (e.g. ``:v0.14.0``) the ``:latest`` tag
    won't move your container — pull the newer tag you actually want, or
    switch to ``:latest`` / ``:main`` for rolling updates.  See available
    tags at https://hub.docker.com/r/nousresearch/hermes-agent/tags
  • Your config and session history live under ``$HERMES_HOME`` (``/opt/data``
    in the container, typically bind-mounted from the host) and persist
    across image upgrades — re-pulling doesn't lose any state.
  • Running a fork?  Build your own image with this repo's ``Dockerfile``
    and replace the ``docker pull`` step with your build/push pipeline."""


def format_docker_update_message() -> str:
    """Return the user-facing message for ``hermes update`` inside Docker."""
    return _DOCKER_UPDATE_MESSAGE


def format_managed_message(action: str = "modify this Hermes installation") -> str:
    """Build a user-facing error for managed installs."""
    managed_system = get_managed_system() or "a package manager"
    return (
        f"Cannot {action}: this Hermes installation is managed by {managed_system}.\n"
        "Use your package manager to upgrade or reinstall Hermes.")


def managed_error(action: str = "modify configuration"):
    """Print user-friendly error for managed mode."""
    print(format_managed_message(action), file=sys.stderr)


def get_container_exec_info() -> Optional[dict]:
    """Read container mode metadata from HERMES_HOME/.container-mode.
    Written by the NixOS activation script when container.enable = true; tells the host CLI to
    exec into the container instead of running locally. None when container mode is off, when
    already inside the container, or when HERMES_DEV=1 is set. Only FileNotFoundError is
    swallowed; other errors (permissions, malformed data) propagate."""
    if os.environ.get("HERMES_DEV") == "1":
        return None

    from hermes_constants import is_container
    if is_container():
        return None

    try:
        info = {}
        with open(get_hermes_home() / ".container-mode", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    info[key.strip()] = value.strip()
    except FileNotFoundError:
        return None

    return {
        "backend": info.get("backend", "docker"),
        "container_name": info.get("container_name", "hermes-agent"),
        "exec_user": info.get("exec_user", "hermes"),
        "hermes_bin": info.get("hermes_bin", "/data/current-package/bin/hermes")}


# ---- Config paths / HERMES_HOME skeleton ----

def get_config_path() -> Path:
    """Get the main config file path."""
    return get_hermes_home() / "config.yaml"


def require_parseable_user_config(*, ignore_user_config: bool = False) -> None:
    """Reject an existing invalid config before a non-interactive agent run.
    Interactive surfaces keep ``load_config()``'s recovery behavior so the operator can repair
    the file; a one-shot run has no such chance, and defaults there could silently pick a hosted
    provider and spend against ``.env`` credentials. Missing/empty files stay valid first-run
    states; ``--ignore-user-config`` / HERMES_IGNORE_USER_CONFIG=1 remain authoritative."""
    if ignore_user_config or os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1":
        return

    config_path = get_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            data = fast_safe_load(f)
    except FileNotFoundError:
        return
    except Exception as exc:
        parse_error = exc
    else:
        if data is None or isinstance(data, dict):
            return
        parse_error = TypeError(f"top-level YAML value must be a mapping, got {type(data).__name__}")

    backup_path = _backup_corrupt_config(config_path)
    message = (
        f"Refusing non-interactive startup because {config_path} is invalid: "
        f"{parse_error}. Repair the file or pass --ignore-user-config to "
        "intentionally run with built-in defaults.")
    if backup_path is not None:
        message += f" A copy was saved to {backup_path}."
    logger.error(message)
    raise InvalidUserConfigError(message) from parse_error


def get_env_path() -> Path:
    """Get the .env file path (for API keys)."""
    return get_hermes_home() / ".env"


def get_project_root() -> Path:
    """Get the project installation directory."""
    return Path(__file__).parent.parent.resolve()


def _resolve_hermes_uid_gid() -> tuple[Optional[int], Optional[int]]:
    """Read HERMES_UID / HERMES_GID (set by Docker deployments); (None, None) if unset/invalid/Windows.
    The entrypoint chowns HERMES_HOME once, but subdirs created at runtime (``profiles/<name>/``)
    need the same chown or they land root:root and block later uid-mapped workers.

    Docker containers running Hermes commonly set these to map the in-container user to a host user so
    volume-mounted state files end up with the right ownership. See #34107.
    """
    if sys.platform == "win32":
        return None, None

    def _env_int(name: str) -> Optional[int]:
        try:
            return int(os.environ.get(name, "").strip() or None)
        except (TypeError, ValueError):
            return None

    return _env_int("HERMES_UID"), _env_int("HERMES_GID")


def _chown_to_hermes_uid(path) -> None:
    """Chown ``path`` to ``HERMES_UID:HERMES_GID`` when set; EPERM/ENOENT are non-fatal (the
    entrypoint's startup chown -R fixes ownership on the next restart).

    Used by :func:`_secure_dir` to keep ownership consistent across all directories created by
    :func:`ensure_hermes_home` on Docker deployments. See #34107.
    """
    uid, gid = _resolve_hermes_uid_gid()
    if uid is None and gid is None:
        return
    try:
        os.chown(path, uid if uid is not None else -1, gid if gid is not None else -1)
    except (OSError, AttributeError, NotImplementedError):
        pass


def _secure_dir(path):
    """chmod a directory owner-only (0700) and apply HERMES_UID/GID ownership. No-op when managed.
    HERMES_HOME_MODE (e.g. 0701) overrides the mode so a web server can traverse HERMES_HOME to
    a served subdirectory without directory listings.

    Also applies ``HERMES_UID``/``HERMES_GID``-based ownership when those env vars are set (#34107 — Docker
    deployments need this so profile subdirs created at runtime by kanban workers don't land as root:root
    and block subsequent uid-mapped workers).
    """
    if is_managed():
        return
    try:
        mode = int(os.environ.get("HERMES_HOME_MODE", "").strip() or "700", 8)
    except ValueError:
        mode = 0o700
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass
    _chown_to_hermes_uid(path)


def _is_container() -> bool:
    """Detect Docker/Podman/LXC (or HERMES_CONTAINER / HERMES_SKIP_CHMOD opt-out).
    Volume-mounted config is not forced to 0o600 in containers: gateway and dashboard may run
    as different UIDs, or the mount itself needs broader permissions."""
    if (os.environ.get("HERMES_CONTAINER") or os.environ.get("HERMES_SKIP_CHMOD")
            or os.path.exists("/.dockerenv")):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            cgroup_content = f.read()
        return any(marker in cgroup_content for marker in ("docker", "lxc", "kubepods"))
    except (OSError, IOError):
        return False


def _secure_file(path):
    """chmod a file 0600. Skipped when managed (activation sets 0640 group-readable) or in a
    container (mounts often need broader permissions)."""
    if is_managed() or _is_container():
        return
    try:
        if os.path.exists(str(path)):
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _ensure_default_soul_md(home: Path) -> None:
    """Seed DEFAULT_SOUL_MD on first run; upgrade a legacy comment-only scaffold in place.
    A SOUL.md the user actually customized is never touched."""
    soul_path = home / "SOUL.md"
    if soul_path.exists():
        try:
            existing = soul_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        if not is_legacy_template_soul(existing):
            return
    soul_path.write_text(DEFAULT_SOUL_MD, encoding="utf-8")
    _secure_file(soul_path)


# Home paths whose directory skeleton was created this process. Only successful passes are
# recorded, so a raised managed-mode/missing-profile error keeps re-checking on later loads.
_HERMES_HOME_ENSURED: set = set()
_HERMES_HOME_SUBDIRS = (
    "cron", "sessions", "logs", "logs/curator", "memories",
    "pairing", "hooks", "image_cache", "audio_cache", "skills")


def ensure_hermes_home():
    """Ensure the ~/.hermes directory skeleton exists with secure permissions.
    Memoized per home path: this runs on EVERY ``load_config()`` and the ~14 mkdir/chmod syscalls
    made repeated loads the dominant cost of hot read paths."""
    home = get_hermes_home()
    key = str(home)

    # Named profiles must be created explicitly. Check tombstones BEFORE the memo so a stale
    # empty shell cannot skip the deleted-profile guard.
    from hermes_constants import assert_named_profile_home_live
    assert_named_profile_home_live(home)
    if key in _HERMES_HOME_ENSURED and home.is_dir():
        return
    if is_managed():
        # Activation creates the dirs; verify, then seed SOUL.md. logs/curator may be unknown to
        # the activation script (inside an already-secured logs/). umask(0o007) => SOUL.md is 0660.
        old_umask = os.umask(0o007)
        try:
            if not home.is_dir():
                raise RuntimeError(f"HERMES_HOME {home} does not exist.")
            for subdir in ("cron", "sessions", "logs", "memories"):
                if not (home / subdir).is_dir():
                    raise RuntimeError(f"{home / subdir} does not exist.")
            (home / "logs" / "curator").mkdir(parents=True, exist_ok=True)
            _ensure_default_soul_md(home)
        finally:
            os.umask(old_umask)
    else:
        home.mkdir(parents=True, exist_ok=True)
        _secure_dir(home)
        for subdir in _HERMES_HOME_SUBDIRS:
            d = home / subdir
            d.mkdir(parents=True, exist_ok=True)
            _secure_dir(d)
        _ensure_default_soul_md(home)

    _HERMES_HOME_ENSURED.add(key)


# ---- Config loading/saving ----

from hermes_cli.config_defaults import DEFAULT_CONFIG, OPTIONAL_ENV_VARS  # noqa: E402,F401
from hermes_cli.config_providers import (  # noqa: E402,F401  (re-exported; callers/tests use hermes_cli.config.<name>)
    _API_MODE_ALIASES, _CAMEL_ALIASES, _KNOWN_PROVIDER_KEYS, _PROVIDER_NORMALIZE_WARNED,
    _canonical_api_mode, _coerce_ssl_verify, _custom_provider_entry_to_provider_config,
    _entries_for_route, _normalize_custom_provider_entry, _normalize_provider_models,
    _pick_provider_base_url, _route_model_cfg, _warn_once_per_provider,
    apply_custom_provider_extra_headers_to_client_kwargs,
    apply_custom_provider_tls_to_client_kwargs, coerce_provider_id, find_provider_entry,
    get_compatible_custom_providers, get_custom_provider_context_length,
    get_custom_provider_extra_headers, get_custom_provider_model_capability,
    get_custom_provider_tls_settings, is_provider_enabled, normalize_extra_headers,
    providers_dict_to_custom_providers, stringify_provider_map)
# Back-compat re-exports — :mod:`hermes_cli.personality` owns personality/overlay semantics.
from hermes_cli.personality import (  # noqa: E402,F401
    NEUTRAL_PERSONALITY_NAMES as _NEUTRAL_PERSONALITY_NAMES,
    prompt_text as _prompt_text,
    render_personality_prompt,
    resolve_ephemeral_system_prompt as resolve_ephemeral_system_prompt_from_config)

# ---- Config Migration System ----

# Env vars introduced per config version; migration only mentions vars new since the user's
# previous version.
def _ensure_hermes_home_managed(home: Path):
    """Managed-mode variant: verify dirs exist (activation creates them), seed SOUL.md."""
    if not home.is_dir():
        raise RuntimeError(
            f"HERMES_HOME {home} does not exist."
        )
    for subdir in ("cron", "sessions", "logs", "memories"):
        d = home / subdir
        if not d.is_dir():
            raise RuntimeError(f"{d} does not exist.")
    # Curator reports dir is a sub-path of logs/; create it if missing.
    # In managed mode the activation script may not know about this subdir,
    # so we mkdir it ourselves (it's inside an already-secured logs/ dir).
    (home / "logs" / "curator").mkdir(parents=True, exist_ok=True)
    # Inside umask(0o007) scope — SOUL.md will be created as 0660
    _ensure_default_soul_md(home)


# =============================================================================
# Config loading/saving
# =============================================================================

from hermes_cli.config_defaults import DEFAULT_CONFIG, OPTIONAL_ENV_VARS  # noqa: F401
DEFAULT_CONFIG = {
    "model": "",
    "providers": {},
    "fallback_providers": [],
    "credential_pool_strategies": {},
    "toolsets": ["hermes-cli"],
    # Global active chat session cap across CLI, TUI/dashboard, and messaging.
    # None/0 = unbounded.
    "max_concurrent_sessions": None,
    # Soft LRU cap on in-memory TUI/desktop/dashboard sessions. When more than
    # this many are live, the gateway evicts the least-recently-active DETACHED
    # sessions (no live client) so accumulated agents don't pile up under memory
    # pressure. Reopening one re-resumes it from disk. 0/null disables.
    "max_live_sessions": 16,
    "agent": {
        "max_turns": 500,
        # Inactivity timeout for gateway agent execution (seconds).
        # The agent can run indefinitely as long as it's actively calling
        # tools or receiving API responses.  Only fires when the agent has
        # been completely idle for this duration.  0 = unlimited.
        "gateway_timeout": 1800,
        # Graceful drain timeout for gateway stop/restart (seconds).
        # The gateway stops accepting new work, waits for running agents
        # to finish, then interrupts any remaining runs after the timeout.
        # 0 = no drain, interrupt immediately (the default).
        #
        # Contract: if you restart the gateway, in-flight work stops. We do
        # not hold the restart open for a grace window — a drain timeout
        # large enough to "save" a long agent turn would have to outlast an
        # unbounded task (some runs take days), which is impossible, and a
        # drain timeout shorter than systemd's TimeoutStopSec invites a
        # SIGKILL-mid-cleanup race that leaves a stale lock and crash-loops
        # the service. 0 sidesteps both: interrupt now, clean up, exit fast.
        # Set a positive value in config.yaml only if you explicitly want a
        # grace window on /restart (and keep it well under TimeoutStopSec).
        "restart_drain_timeout": 0,
        # Upper bound (seconds) a submitted prompt waits for the deferred
        # agent build (MCP discovery, model metadata, skills scan) before
        # failing with a visible error (#63078). The gateway's wait is
        # patient — the prompt is delivered the moment the build completes
        # and a progress notice is emitted past 30s — so this cap only fires
        # on a genuinely hung build. Raise it for deployments with many slow
        # or unreachable MCP servers.
        "build_wait_timeout": 600,
        # Max app-level retry attempts for API errors (connection drops,
        # provider timeouts, 5xx, etc.) before the agent surfaces the
        # failure.  The OpenAI SDK already does its own low-level retries
        # (max_retries=2 default) for transient network errors; this is
        # the Hermes-level retry loop that wraps the whole call.  Lower
        # this to 1 if you use fallback providers and want fast failover
        # on flaky primaries; raise it if you prefer to tolerate longer
        # provider hiccups on a single provider.
        "api_max_retries": 3,
        "service_tier": "",
        # Tool-use enforcement: injects system prompt guidance that tells the
        # model to actually call tools instead of describing intended actions.
        # Values: "auto" (default — applies to gpt/codex models), true/false
        # (force on/off for all models), or a list of model-name substrings
        # to match (e.g. ["gpt", "codex", "gemini", "qwen"]).
        "tool_use_enforcement": "auto",
        # Intent-ack continuation: when the model opens a turn by narrating an
        # action it will take ("I'll go check the logs...") but emits no tool
        # call, intercept the turn-end, inject a "continue now, execute the
        # tools" nudge, and loop instead of ending the turn (capped at 2 nudges
        # per turn). This is the corrective sibling of tool_use_enforcement (the
        # preventive prompt-side guard). Values: "auto" (default — fires only on
        # the codex_responses api_mode, the historical behavior), true (all
        # api_modes — fixes the Gemini/Claude "stops after stating intent" case),
        # false (never), or a list of model-name substrings to match.
        "intent_ack_continuation": "auto",
        # Universal "finish the job" guidance — short prompt block applied to
        # all models that targets two cross-family failure modes: (1) stopping
        # after a stub instead of finishing the artifact, (2) fabricating
        # plausible-looking output when a real path is blocked.  Costs ~80
        # tokens in the cached system prompt.  Set False to disable globally.
        "task_completion_guidance": True,
        # Universal parallel-tool-call guidance — short prompt block applied to
        # all models that tells the model to batch independent tool calls
        # (reads, searches, web fetches, read-only commands) into one turn
        # instead of one call per turn.  The runtime already runs independent
        # calls concurrently, so this just steers the model to produce the
        # batch — cutting round-trips and the resent-context cost that
        # compounds over a long conversation.  Costs ~70 tokens in the cached
        # system prompt.  Set False to disable globally.
        "parallel_tool_call_guidance": True,
        # Local-environment toolchain probe — surfaces Python/pip/uv/PEP-668
        # state in the system prompt when something non-default is detected
        # (e.g. python3 has no pip module, pip→python version mismatch, PEP
        # 668 enforcement without uv).  Costs zero tokens when the env is
        # clean (probe emits nothing).  Skipped for remote terminal backends
        # (docker/modal/ssh — they have their own probe).  Set False to
        # disable entirely.
        "environment_probe": True,
        # Embedder-supplied environment description appended to the system
        # prompt's environment-hints block. Lets a host that wraps Hermes
        # (sandbox runner, managed platform) explain the runtime environment
        # — proxy, credential handling, mount layout — without editing the
        # identity slot (SOUL.md). Empty by default. The HERMES_ENVIRONMENT_HINT
        # env var overrides this (build-time/container mechanism).
        "environment_hint": "",
        # Coding posture — on interactive coding surfaces (CLI, TUI, desktop
        # app, ACP) in a code workspace, Hermes adds a coding operating brief
        # + a live git/workspace snapshot to the system prompt. See
        # agent/coding_context.py.
        #   "auto" (default) — prompt-only posture when the surface is
        #                      interactive AND cwd is a code workspace.
        #                      Toolsets are never touched; messaging platforms
        #                      unaffected.
        #   "focus"          — auto + collapse the toolset to the lean coding
        #                      set (+ enabled MCP servers) + demote non-coding
        #                      skill categories to names-only in the prompt's
        #                      skill index. Explicit opt-in.
        #   "on"             — force the prompt posture everywhere.
        #   "off"            — disable entirely.
        "coding_context": "auto",
        # Standing operator instructions for the coding posture. A string (or
        # list of strings) appended to the coding brief as an extra stable
        # system block — pin project-wide workflow rules here instead of editing
        # the shipped brief, e.g. "For UI work, don't run tsc/lint until I
        # approve. Clean the diff before you commit and push." Cache-safe:
        # takes effect next session. Empty by default.
        "coding_instructions": "",
        # When verify-on-stop finds edited code without fresh verification
        # evidence, append guidance for creative UI work (avoid broad
        # tsc/lint/test before visual approval) and clean-diff expectations.
        # Set false to keep the evidence nudge terse.
        "verify_guidance": True,
        # Upper bound on consecutive `pre_verify` "continue" nudges in a single
        # turn, so a user/plugin hook can never trap the loop.
        "max_verify_nudges": 3,
        # Verification closure: after the agent edits files in a code workspace,
        # do not accept a final answer until fresh verification evidence exists
        # or the agent explains why it cannot run checks. The loop is bounded
        # and uses the passive verification ledger. Default is "auto" —
        # surface-aware: on for interactive coding surfaces (CLI, TUI, desktop)
        # and programmatic callers, off for conversational messaging surfaces
        # (Telegram, Discord, etc.) where the verification narrative would reach
        # a human as chat noise. Doc/markdown/skill-only edits never fire it.
        # Set true to force on everywhere, or false to disable.
        "verify_on_stop": "auto",
        # Staged inactivity warning: send a warning to the user at this
        # threshold before escalating to a full timeout.  The warning fires
        # once per run and does not interrupt the agent.  0 = disable warning.
        "gateway_timeout_warning": 900,
        # Maximum time (seconds) the gateway will block an agent waiting for
        # a clarify-tool response from the user.  Hit this and the agent
        # unblocks with "[user did not respond within Xm]" so it can adapt
        # rather than pinning the running-agent guard forever.  CLI clarify
        # blocks indefinitely (input() is synchronous) and ignores this.
        # Default 3600 (1h): real users step away (meetings, AFK) and the
        # old 600s default evicted the entry mid-think, so a later button
        # tap landed on a dead entry (#32762).  Tradeoff: a higher value
        # holds the gateway's running-agent guard longer for a genuinely
        # abandoned prompt — lower it if a single session must free up the
        # guard sooner.
        "clarify_timeout": 3600,
        # Periodic "still working" notification interval (seconds).
        # Sends a status message every N seconds so the user knows the
        # agent hasn't died during long tasks.  0 = disable notifications.
        # Lower values mean faster feedback on slow tasks but more chat
        # noise; 180s is a compromise that catches spinning weak-model runs
        # (60+ tool iterations with tiny output) before users assume the
        # bot is dead and /restart.
        "gateway_notify_interval": 180,
        # Freshness window for the gateway auto-continue note (seconds).
        # After a gateway crash/restart/SIGTERM mid-run, the next user
        # message gets a "[System note: your previous turn was
        # interrupted — process the unfinished tool result(s) first]"
        # prepended so the model picks up where it left off.  That's the
        # right behaviour while the interruption is fresh, but stale
        # markers (transcript last touched hours or days ago) can revive
        # an unrelated old task when the user's next message starts new
        # work.  This window is the max age of the last persisted
        # transcript row for which we still inject the continue note.
        # Default 3600s comfortably covers a long turn (gateway_timeout
        # default is 1800s) plus runtime slack.  Set to 0 to disable the
        # gate and restore pre-fix behaviour (always inject).
        "gateway_auto_continue_freshness": 3600,
        # Max seconds the gateway waits for boot auto-resume turns to finish
        # before it releases the startup-restore inbound gate.  While startup
        # restore is in progress the gateway QUEUES every inbound message
        # instead of replying, so no channel gets an answer until this gate
        # opens.  Without a bound, one pathologically long resumed turn holds
        # the gate shut and every channel's inbound piles up unanswered for as
        # long as that turn runs.  On timeout the gate releases and the slow
        # resume turn keeps running in the background; duplicate-agent
        # protection is unaffected because the resume slot is claimed
        # synchronously before the gate runs.  Set to 0 to disable the bound
        # (historical "wait forever" behaviour).
        "gateway_startup_restore_drain_timeout": 30,
        # Stale-stream ceiling for local providers (Ollama, oMLX, llama-cpp) in
        # seconds. When the base stale timeout is at its default (180s) and a
        # local endpoint is detected, this finite ceiling replaces the former
        # infinite disable so a wedged local server eventually trips the
        # detector instead of hanging forever. The env var
        # ``HERMES_LOCAL_STREAM_STALE_TIMEOUT`` overrides for escape-hatch use.
        "local_stream_stale_timeout": 900,
        # How user-attached images are presented to the main model on each turn.
        #   "auto"   — attach natively when the active model reports
        #              supports_vision=True AND the user hasn't explicitly
        #              configured auxiliary.vision.provider.  Otherwise fall
        #              back to text (vision_analyze pre-analysis).
        #   "native" — always attach natively; non-vision models will either
        #              error at the provider or get a last-chance text fallback
        #              (see run_agent._prepare_messages_for_api).
        #   "text"   — always pre-analyze with vision_analyze and prepend the
        #              description as text; the main model never sees pixels.
        # Affects gateway platforms, the TUI, and CLI /attach.  vision_analyze
        # remains available as a tool regardless of this setting — the routing
        # only controls how inbound user images are presented.
        "image_input_mode": "auto",
        "disabled_toolsets": [],

        # Per-model reasoning effort overrides (spelling-tolerant).
        # Dict mapping model names (any reasonable spelling) to effort levels.
        # Takes precedence over agent.reasoning_effort when the current model
        # matches a key in this dict.
        # Edit directly in config.yaml (no CLI support due to dots in keys).
        "reasoning_overrides": {},
    },

    "terminal": {
        "backend": "local",
        "modal_mode": "auto",
        "cwd": ".",  # Use current directory
        "timeout": 180,
        # Bounded grace period (seconds) between SIGTERM and an escalated
        # SIGKILL when terminating a host process tree (browser daemons, etc.).
        # A daemon that stalls in its SIGTERM handler is force-killed after this
        # window so it can't leak indefinitely. 0 disables escalation (SIGTERM
        # only — the historical behavior). Floored internally at 0.
        "daemon_term_grace_seconds": 2.0,
        # Environment variables to pass through to sandboxed execution
        # (terminal and execute_code).  Skill-declared required_environment_variables
        # are passed through automatically; this list is for non-skill use cases.
        "env_passthrough": [],
        # HOME handling for host tool subprocesses:
        #   auto    — host keeps the real OS-user HOME; containers use
        #             HERMES_HOME/home for persistent state (default)
        #   real    — force the real OS-user HOME
        #   profile — force HERMES_HOME/home when it exists (old strict
        #             per-profile CLI config isolation)
        "home_mode": "auto",
        # Extra files to source in the login shell when building the
        # per-session environment snapshot.  Use this when tools like nvm,
        # pyenv, asdf, or custom PATH entries are registered by files that
        # a bash login shell would skip — most commonly ``~/.bashrc``
        # (bash doesn't source bashrc in non-interactive login mode) or
        # zsh-specific files like ``~/.zshrc`` / ``~/.zprofile``.
        # Paths support ``~`` / ``${VAR}``. Missing files are silently
        # skipped. When empty, Hermes auto-sources ``~/.profile``,
        # ``~/.bash_profile``, and ``~/.bashrc`` (in that order) if the
        # snapshot shell is bash (this is the ``auto_source_bashrc``
        # behaviour — disable with that key if you want strict login-only
        # semantics).
        "shell_init_files": [],
        # When true (default), Hermes sources the user's shell rc files
        # (``~/.profile``, ``~/.bash_profile``, ``~/.bashrc``) in the
        # login shell used to build the environment snapshot. This
        # captures PATH additions, shell functions, and aliases — which a
        # plain ``bash -l -c`` would otherwise miss because bash skips
        # bashrc in non-interactive login mode, and because a default
        # Debian/Ubuntu ``~/.bashrc`` short-circuits on non-interactive
        # sources. ``~/.profile`` and ``~/.bash_profile`` are tried first
        # because ``n`` / ``nvm`` / ``asdf`` installers typically write
        # their PATH exports there without an interactivity guard. Turn
        # this off if your rc files misbehave when sourced
        # non-interactively (e.g. one that hard-exits on TTY checks).
        "auto_source_bashrc": True,
        "docker_image": "nikolaik/python-nodejs:python3.11-nodejs20",
        "docker_forward_env": [],
        # Explicit environment variables to set inside Docker containers.
        # Unlike docker_forward_env (which reads values from the host process),
        # docker_env lets you specify exact key-value pairs — useful when Hermes
        # runs as a systemd service without access to the user's shell environment.
        # Example: {"SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.sock"}
        "docker_env": {},
        "singularity_image": "docker://nikolaik/python-nodejs:python3.11-nodejs20",
        "modal_image": "nikolaik/python-nodejs:python3.11-nodejs20",
        "daytona_image": "nikolaik/python-nodejs:python3.11-nodejs20",
        # Container resource limits (docker, singularity, modal, daytona — ignored for local/ssh)
        "container_cpu": 1,
        "container_memory": 5120,       # MB (default 5GB)
        "container_disk": 51200,        # MB (default 50GB)
        "container_persistent": True,   # Persist filesystem across sessions
        # Docker volume mounts — share host directories with the container.
        # Each entry is "host_path:container_path" (standard Docker -v syntax).
        # Example:
        # ["/home/user/projects:/workspace/projects",
        #  "/home/user/.hermes/cache/documents:/output"]
        # For gateway MEDIA delivery, write inside Docker to /output/... and emit
        # the host-visible path in MEDIA:, not the container path.
        "docker_volumes": [],
        # Explicit opt-in: mount the host cwd into /workspace for Docker sessions.
        # Default off because passing host directories into a sandbox weakens isolation.
        "docker_mount_cwd_to_workspace": False,
        # Opt-in egress lockdown for Docker terminal sessions. When false,
        # Docker runs with --network=none so commands cannot reach the network.
        "docker_network": True,
        "docker_extra_args": [],        # Extra flags passed verbatim to docker run
        # Explicit opt-in: run the Docker container as the host user's uid:gid
        # (via `--user`).  When enabled, files written into bind-mounted dirs
        # (docker_volumes, the persistent workspace, or the auto-mounted cwd)
        # are owned by your host user instead of root, which avoids needing
        # `sudo chown` after container runs. Default off to preserve behavior
        # for images whose entrypoints expect to start as root (e.g. the
        # bundled Hermes image, which drops to the `hermes` user via
        # s6-setuidgid inside each supervised service).
        # When on, SETUID/SETGID caps are omitted from the container since
        # no privilege drop is needed.
        "docker_run_as_host_user": False,
        # Persistent shell — keep a long-lived bash shell across execute() calls
        # so cwd/env vars/shell variables survive between commands.
        # Enabled by default for non-local backends (SSH); local is always opt-in
        # via TERMINAL_LOCAL_PERSISTENT env var.
        "persistent_shell": True,
    },

    "web": {
        "backend": "",           # shared fallback — applies to both search and extract
        "search_backend": "",    # per-capability override for web_search (e.g. "searxng")
        "extract_backend": "",   # per-capability override for web_extract (e.g. "native")
        "extract_char_limit": 15000,  # per-page char budget for web_extract; larger pages truncate + store full text in cache/web
    },

    "browser": {
        "inactivity_timeout": 120,
        "command_timeout": 30,  # Timeout for browser commands in seconds (screenshot, navigate, etc.)
        "record_sessions": False,  # Auto-record browser sessions as WebM videos
        "headed": False,  # Local mode: launch Chromium with a visible window (also skips per-turn cleanup so the window persists between turns; idle reaper still applies)
        "allow_private_urls": False,  # Allow navigating to private/internal IPs (localhost, 192.168.x.x, etc.)
        # Browser engine for local mode.  Passed as ``--engine <value>`` to
        # agent-browser v0.25.3+.
        # "auto"       — use Chrome (default, don't pass --engine at all)
        # "lightpanda" — use Lightpanda (1.3-5.8x faster navigation, no screenshots)
        # "chrome"     — explicitly request Chrome
        # Also settable via AGENT_BROWSER_ENGINE env var.
        "engine": "auto",
        "auto_local_for_private_urls": True,  # When a cloud provider is set, auto-spawn local Chromium for LAN/localhost URLs instead of sending them to the cloud
        "cdp_url": "",  # Optional persistent CDP endpoint for attaching to an existing Chromium/Chrome
        "allow_unsafe_evaluate": False,  # Legacy override: when true, browser_console(expression=...) bypasses the restrict_evaluate denylist entirely
        "restrict_evaluate": False,  # Opt-in denylist blocking sensitive JS primitives (cookies/storage/clipboard/network/form values) in browser_console(expression=...)
        # CDP supervisor — dialog + frame detection via a persistent WebSocket.
        # Active only when a CDP-capable backend is attached (Browserbase or
        # local Chrome via /browser connect). See
        # website/docs/developer-guide/browser-supervisor.md.
        "dialog_policy": "must_respond",  # must_respond | auto_dismiss | auto_accept
        "dialog_timeout_s": 300,  # Safety auto-dismiss after N seconds under must_respond
        "camofox": {
            # When true, Hermes sends a stable profile-scoped userId to Camofox
            # so the server maps it to a persistent Firefox profile automatically.
            # When false (default), each session gets a random userId (ephemeral).
            "managed_persistence": False,
            # Optional externally managed Camofox identity. Useful when another
            # app owns the visible browser and Hermes should operate in it.
            "user_id": "",
            "session_key": "",
            # Rehydrate tab_id from Camofox before creating a new tab.
            "adopt_existing_tab": False,
            # Docker Camofox opens page URLs from inside the container. Enable
            # this to rewrite loopback page URLs (localhost/127.0.0.1/::1) to a
            # host alias while leaving CAMOFOX_URL itself unchanged.
            "rewrite_loopback_urls": False,
            "loopback_host_alias": "host.docker.internal",
        },
    },

    # Filesystem checkpoints — automatic snapshots before destructive file ops.
    # When enabled, the agent takes a snapshot of the working directory once
    # per conversation turn (on first write_file/patch call).  Use /rollback
    # to restore.
    #
    # Defaults changed in v2 (single shared shadow store, real pruning):
    #   - enabled: True -> False   (opt-in; most users never use /rollback)
    #   - max_snapshots: 50 -> 20  (now actually enforced via ref rewrite)
    #   - auto_prune:   False -> True (orphans/stale pruned automatically)
    # Opt in via ``hermes chat --checkpoints`` or set enabled=True here.
    "checkpoints": {
        "enabled": False,
        # Max checkpoints to keep per working directory.  Pre-v2 this only
        # limited the `/rollback` listing; v2 actually rewrites the ref and
        # garbage-collects older commits.
        "max_snapshots": 20,
        # Hard ceiling on total ``~/.hermes/checkpoints/`` size (MB).  When
        # exceeded, the oldest checkpoint per project is dropped in a
        # round-robin pass until total size falls under the cap.
        # 0 disables the size cap.
        "max_total_size_mb": 500,
        # Skip any single file larger than this when staging a checkpoint.
        # Prevents accidental snapshotting of datasets, model weights, and
        # other large generated assets.  0 disables the filter.
        "max_file_size_mb": 10,
        # Auto-maintenance: hermes sweeps the checkpoint base at startup
        # (at most once per ``min_interval_hours``) and:
        #   * deletes project entries whose last_touch is older than
        #     ``retention_days``
        #   * GCs the single shared store to reclaim unreachable objects
        #   * enforces ``max_total_size_mb`` across remaining projects
        #   * deletes ``legacy-*`` archives older than ``retention_days``
        #
        # NOTE: this automatic sweep never deletes "orphan" entries (workdir
        # no longer found on disk). A missing workdir at startup is
        # ambiguous — it can mean the project was deleted, or that an
        # external volume / network share / VPN is simply not mounted yet —
        # and this sweep runs unattended, so it must never guess. Orphan
        # cleanup is only available via the explicit
        # ``hermes checkpoints prune`` command (add ``--keep-orphans`` to
        # skip it), where a human is looking at the output.
        "auto_prune": True,
        "retention_days": 7,
        "min_interval_hours": 24,
    },

    # Hard cap (chars) for a single automatic context file such as SOUL.md,
    # AGENTS.md, CLAUDE.md, .hermes.md, or .cursorrules before Hermes applies
    # head/tail truncation. ``null`` (the default) lets the cap scale with the
    # model's context window (floor 20K, ceiling 500K) so large-context models
    # rarely truncate a project doc. Set a positive integer to pin a fixed cap
    # and override the dynamic behavior. Separate from read_file tool limits.
    "context_file_max_chars": None,

    # Maximum characters returned by a single read_file call.  Reads that
    # exceed this are rejected with guidance to use offset+limit.
    # 100K chars ≈ 25–35K tokens across typical tokenisers.
    "file_read_max_chars": 100_000,

    # Seconds to wait at agent-build time for in-flight MCP server discovery
    # to finish before the agent snapshots its tool list.  MCP discovery runs
    # in a background thread so a slow/dead server can't freeze startup; this
    # bounds how long the first agent build blocks on it.  The wait returns
    # the INSTANT discovery completes, so users with no MCP servers (the common
    # case) or fast servers pay ~0s regardless of this value — the bound is
    # only reached when a server is genuinely still connecting.  The old 0.75s
    # default was a touch short for HTTP/OAuth servers on a cold connect; a
    # modest bump lets more of them land in the FIRST turn's snapshot.  This is
    # only a turn-1 latency/UX knob: a server that misses this window is still
    # picked up automatically on the next turn by the between-turns refresh
    # (see agent/turn_context.py), so correctness never depends on it.  Keep it
    # small so a slow/dead server adds little to first-response latency.
    "mcp_discovery_timeout": 1.5,

    # MCP runtime behavior (distinct from the per-server definitions in
    # mcp_servers: and from the auxiliary.mcp side-LLM task settings).
    "mcp": {
        # Auto-reload MCP connections when config.yaml's mcp_servers section
        # changes at runtime (CLI file watcher, default on).
        # Set to false to stop the automatic reload: every automatic reload
        # rebuilds the agent tool surface and INVALIDATES the provider
        # prompt cache (the next message re-sends the full input prefix),
        # which is expensive on long-context / high-reasoning models.
        # When disabled, the watcher still detects the change and prints
        # guidance to apply it deliberately via /reload-mcp.
        "auto_reload_on_config_change": True,
    },

    # Tool-output truncation thresholds. When terminal output or a
    # single read_file page exceeds these limits, Hermes truncates the
    # payload sent to the model (keeping head + tail for terminal,
    # enforcing pagination for read_file). Tuning these trades context
    # footprint against how much raw output the model can see in one
    # shot. Ported from anomalyco/opencode PR #23770.
    #
    # - max_bytes:       terminal_tool output cap, in chars
    #                    (default 50_000 ≈ 12-15K tokens).
    # - max_lines:       read_file pagination cap — the maximum `limit`
    #                    a single read_file call can request before
    #                    being clamped (default 2000).
    # - max_line_length: per-line cap applied when read_file emits a
    #                    line-numbered view (default 2000 chars).
    "tool_output": {
        "max_bytes": 50_000,
        "max_lines": 2000,
        "max_line_length": 2000,
    },

    # Tool loop guardrails nudge models when they repeat failed or
    # non-progressing tool calls. Soft warnings are always-on by default;
    # hard stops are opt-in so interactive CLI/TUI sessions keep flowing.
    "tool_loop_guardrails": {
        "warnings_enabled": True,
        "hard_stop_enabled": False,
        "warn_after": {
            "exact_failure": 2,
            "same_tool_failure": 3,
            "idempotent_no_progress": 2,
        },
        "hard_stop_after": {
            "exact_failure": 5,
            "same_tool_failure": 8,
            "idempotent_no_progress": 5,
        },
        # Per-turn runaway-loop caps (inspired by Claude Code v2.1.212,
        # Week 29, July 2026). Hard ceilings on how many times a runaway-prone
        # tool may be called within a SINGLE agent loop (turn); the counters
        # reset at the start of every turn, so a legitimate multi-turn session
        # is never starved. They are always-on and fire regardless of the
        # warn/hard-stop thresholds above. A single turn issuing dozens of web
        # searches or spawning dozens of subagents is already pathological, so
        # the defaults are low. Set either to 0 to disable that cap (unlimited).
        "loop_caps": {
            "max_web_searches": 50,   # max web_search calls per turn (0 = unlimited)
            "max_subagents": 50,      # max subagents spawned per turn (0 = unlimited)
        },
    },

    "compression": {
        "enabled": True,
        "progress_notices": False,    # opt-in (#52995): when True, routine compression
                                      # progress statuses (compacting/preflight/pre-API/
                                      # idle/retry) are delivered to chat gateway
                                      # platforms instead of being suppressed by the
                                      # gateway noise filter. Default False keeps
                                      # routine compression silent-by-design on chat
                                      # surfaces (server-side logging only). Failure
                                      # notices and manual /compress feedback are
                                      # always visible regardless of this setting.
        "threshold": 0.50,            # compress when context usage exceeds this ratio.
                                      # Models with context windows below 512K are
                                      # floored at 0.75 (raise-only) so compaction
                                      # doesn't fire with half the window still free;
                                      # set this above 0.75 to override the floor.
        "threshold_tokens": None,     # absolute token cap — when set, compression
                                      # triggers at the lower of the ratio-based
                                      # threshold and this token count. Clamped to
                                      # the model's context length at apply-time.
        "target_ratio": 0.20,         # fraction of threshold to preserve as recent tail
        "protect_last_n": 20,         # minimum recent messages to keep uncompressed
        "min_tail_user_messages": 1,  # REAL (actionable) user messages guaranteed to
                                      # survive in the uncompressed tail. 1 = existing
                                      # single last-user anchor (default, behavior-
                                      # preserving); raise to e.g. 3 to keep the last
                                      # 3 real user turns verbatim when bulky tool
                                      # outputs fill the tail token budget.
        "max_attempts": 3,            # compression retry rounds before a turn gives up
                                      # with "max compression attempts reached". Raise
                                      # (e.g. 6) for tool-schema-heavy sessions where 3
                                      # rounds cannot clear the request estimate.
                                      # Validated >= 1, hard-capped at 10.
        "proactive_prune_tokens": 0,  # opt-in trigger (tokens) for the deterministic,
                                      # no-LLM tool-result prune, run independently of
                                      # `threshold` above. On large-window models
                                      # `threshold` (≈50% of the window) rarely fires,
                                      # so old tool output otherwise rides in history
                                      # and is re-sent every turn; a low value like
                                      # 48000 reclaims it early. 0 = off. Recent tail
                                      # protected by `protect_last_n`. Built-in
                                      # compressor only (other engines inherit a no-op).
                                      # NOTE: each committed prune rewrites already-sent
                                      # history, breaking the provider prompt-cache
                                      # prefix — the min_reclaim gate below keeps those
                                      # breaks episodic rather than per-turn.
        "proactive_prune_min_result_chars": 8000,  # the prune's summarize pass only
                                      # touches tool results larger than this (chars);
                                      # clamped to >= 200 so a generated summary can't
                                      # itself be re-summarized.
        "proactive_prune_min_reclaim_tokens": 4096,  # a proactive prune only commits
                                      # when it reclaims at least this many tokens
                                      # (measured on the pruned output). Keeps
                                      # prompt-cache invalidation amortized: one big
                                      # episodic break instead of a tiny break every
                                      # tool iteration. 0 = commit any non-zero prune.
        "hygiene_hard_message_limit": 5000,  # gateway session-hygiene force-compress threshold by message count
        "hygiene_timeout_seconds": 30,  # max seconds gateway waits for pre-agent hygiene compression
                                      # WITHOUT forward progress. The summary call streams, so
                                      # this is an inactivity budget: a slow model still
                                      # producing tokens keeps extending the wait; only a
                                      # silent/hung call is cut off.
        "hygiene_total_ceiling_seconds": 600,  # absolute cap on the hygiene compression wait even
                                      # while tokens are still moving — bounds a degenerate
                                      # trickle stream. Clamped to >= hygiene_timeout_seconds.
        "hygiene_failure_cooldown_seconds": 300,  # skip repeated failed hygiene attempts for this session
        "protect_first_n": 3,         # non-system head messages always preserved
                                      # verbatim, in ADDITION to the system prompt
                                      # (which is always implicitly protected). Set to
                                      # 0 for long-running rolling-compaction sessions
                                      # where you want nothing pinned except the
                                      # system prompt + rolling summary + recent tail.
        "abort_on_summary_failure": False,  # When True, auto-compression that fails
                                      # to generate a summary (aux LLM errored / returned
                                      # non-JSON / timed out) aborts entirely instead of
                                      # dropping the middle window with a static
                                      # "summary unavailable" placeholder.  Messages are
                                      # preserved unchanged and the session "freezes" at
                                      # its current size until the user runs /compress
                                      # (which bypasses the failure cooldown) or /new.
                                      # Default False matches historical behavior; set to
                                      # True if you'd rather pause than silently lose
                                      # context turns when your aux model is flaky.
        "codex_gpt55_autoraise": True,  # Historical key name kept for compatibility.
                                      # When True, gpt-5.4 / gpt-5.5 / gpt-5.6 on the
                                      # ChatGPT Codex OAuth route raise their compaction
                                      # trigger to 85% (vs the global `threshold` above).
                                      # Codex hard-caps these families at a 272K window, so
                                      # the default 50% would compact at ~136K and waste half
                                      # the usable context. Set to False to opt back down to
                                      # the global threshold (e.g. 0.50) for those Codex
                                      # sessions. Only this exact route is affected —
                                      # gpt-5.4 / 5.5 / 5.6 on OpenAI's direct API,
                                      # OpenRouter, and Copilot keep the global threshold
                                      # regardless.
        "codex_gpt55_autoraise_notice": True,  # Display the one-time Codex gpt-5.4/5.5/5.6
                                      # autoraise banner. Set False to keep the
                                      # 85% threshold autoraise but suppress the
                                      # user-facing notice in CLI/gateway output.
        "codex_app_server_auto": "native",  # Codex app-server (codex CLI runtime) thread
                                      # compaction mode. The codex agent owns the real
                                      # thread context, so Hermes' summarizer cannot
                                      # shrink it (#36801). native = codex decides when
                                      # to compact its own thread (default); hermes =
                                      # Hermes' compression threshold triggers
                                      # thread/compact/start; off = never auto-trigger
                                      # (codex may still compact natively).
        "in_place": True,             # When True, compaction rewrites the message
                                      # list and rebuilds the system prompt WITHOUT
                                      # rotating the session id — the conversation
                                      # keeps one durable id for its whole life
                                      # (no parent_session_id chain, no `name #N`
                                      # renumbering). Eliminates the session-rotation
                                      # bug cluster (#33618 /goal loss, #14238 lost
                                      # response, #33907 orphans, #45117 search gaps,
                                      # #42228 null cwd) — see #38763. Non-destructive:
                                      # the live context is compacted (lossy for what
                                      # the model reloads), but the pre-compaction
                                      # turns are soft-archived under the same id
                                      # (active=0, compacted=1) — still searchable via
                                      # session_search and recoverable, not deleted.
                                      # Default True since 2107b86024; set False to
                                      # restore the legacy rotating-compaction path.
        "model_thresholds": {},       # Per-model threshold overrides. Keys are
                                      # substring-matched against the model name
                                      # (longest match wins); values replace the
                                      # global `threshold` for that model, e.g.
                                      #   model_thresholds:
                                      #     "glm-5.2": 0.40
                                      #     "claude-sonnet": 0.35
                                      # The small-context floor (0.75 for <512K
                                      # models) still applies on top of overrides
                                      # (raise-only: an override above the floor
                                      # wins; one below it is raised to the floor).
        "idle_compact_after_seconds": 0,  # Opt-in idle compaction (0 = disabled).
                                      # When > 0, a session that resumes after at
                                      # least this many seconds of inactivity
                                      # compacts its accumulated history up front,
                                      # before the first reply — so a long-lived
                                      # thread resumed hours later doesn't re-read
                                      # its full stale context on every turn.
                                      # Time-based; complements (does not replace)
                                      # the size-based `threshold` above. Skipped
                                      # when the context is already at/below the
                                      # post-compression target (threshold ×
                                      # target_ratio) and it honors the same
                                      # failure-cooldown / anti-thrash / per-session
                                      # lock guards as every automatic compaction.
                                      # Example: 1800 = compact after 30 min idle.
        "relevance_pinning": {       # Optional lexical MVP: select reference-only
            "enabled": False,        # older middle-window excerpts for the summarizer.
            "max_pins": 8,
            "max_pin_chars_total": 12000,
            "min_score": 3,
        },
    },

    # Anthropic prompt caching (Claude via OpenRouter or native Anthropic API).
    # cache_ttl must be "5m" or "1h" (Anthropic-supported tiers); other values are ignored.
    "prompt_caching": {
        "cache_ttl": "5m",
    },

    # OpenRouter-specific settings.
    # response_cache: enable OpenRouter response caching (X-OpenRouter-Cache header).
    #   When enabled, identical requests return cached responses for free (zero billing).
    #   This is separate from Anthropic prompt caching and works alongside it.
    #   See: https://openrouter.ai/docs/guides/features/response-caching
    # response_cache_ttl: how long cached responses remain valid, in seconds (1-86400).
    #   Default 300 (5 minutes). Only used when response_cache is enabled.
    # min_coding_score: knob for the openrouter/pareto-code router (0.0-1.0).
    #   Only applied when model.model is "openrouter/pareto-code". Higher
    #   values route to stronger (more expensive) coders; lower values open
    #   up cheaper, faster options. Default 0.65 lands on the mid-tier
    #   coder on the current Pareto frontier. Empty string = let OpenRouter
    #   pick the strongest available coder (router's documented default
    #   when the plugins block is omitted).
    #   See: https://openrouter.ai/docs/guides/routing/routers/pareto-router
    "openrouter": {
        "response_cache": True,
        "response_cache_ttl": 300,
        "min_coding_score": 0.65,
    },

    # AWS Bedrock provider configuration.
    # Only used when model.provider is "bedrock".
    "bedrock": {
        "region": "",  # AWS region for Bedrock API calls (empty = AWS_REGION env var → us-east-1)
        "discovery": {
            "enabled": True,           # Auto-discover models via ListFoundationModels
            "provider_filter": [],     # Only show models from these providers (e.g. ["anthropic", "amazon"])
            "refresh_interval": 3600,  # Cache discovery results for this many seconds
        },
        "guardrail": {
            # Amazon Bedrock Guardrails — content filtering and safety policies.
            # Create a guardrail in the Bedrock console, then set the ID and version here.
            # See: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html
            "guardrail_identifier": "",  # e.g. "abc123def456"
            "guardrail_version": "",     # e.g. "1" or "DRAFT"
            "stream_processing_mode": "async",  # "sync" or "async"
            "trace": "disabled",         # "enabled", "disabled", or "enabled_full"
        },
    },

    # Auxiliary model config — provider:model for each side task.
    # Format: provider is the provider name, model is the model slug.
    # "auto" for provider = auto-detect best available provider.
    # Empty model = use provider's default auxiliary model.
    # All tasks fall back to openrouter:google/gemini-3-flash-preview if
    # the configured provider is unavailable.
    #
    # extra_body: forwarded verbatim as request body fields on every aux call
    # for that task. Use this to set provider-specific knobs (independent of
    # main-agent settings). On OpenRouter you can set provider routing prefs
    # and the Pareto Code coding-score floor here. Example:
    #
    #   auxiliary:
    #     compression:
    #       provider: openrouter
    #       model: openrouter/pareto-code
    #       extra_body:
    #         provider:           # OpenRouter provider routing
    #           order: [anthropic, google]
    #           sort: throughput  # or price | latency
    #         plugins:            # OpenRouter Pareto Code router
    #           - id: pareto-router
    #             min_coding_score: 0.5
    #
    # Each aux task is independent — main-agent provider_routing and
    # openrouter.min_coding_score do NOT propagate to aux calls by design.
    "auxiliary": {
        # Same-provider retries for a transient transport blip (connection
        # reset / timeout / 5xx / 408) on ANY auxiliary call before falling
        # back. Default 2 (→ 3 total attempts), clamped [0,6]. Matters most for
        # pinned calls like MoA reference advisors, where provider fallback is
        # not a meaningful recovery, so an unretried blip silently loses the
        # call.
        "transient_retries": 2,
        # Endpoints that reject NON-streaming chat requests outright (e.g.
        # Tencent Copilot returns HTTP 400 "Non-stream chat request is
        # currently not supported"). Auxiliary calls to a matching endpoint
        # are sent with stream=True and aggregated client-side. Entries are
        # case-insensitive substrings matched against the endpoint URL;
        # copilot.tencent.com is always treated as stream-only.
        "stream_only_base_urls": [],
        "vision": {
            "provider": "auto",    # auto | openrouter | nous | codex | custom
            "model": "",           # e.g. "google/gemini-2.5-flash", "gpt-4o"
            "base_url": "",        # direct OpenAI-compatible endpoint (takes precedence over provider)
            "api_key": "",         # API key for base_url (falls back to OPENAI_API_KEY)
            "timeout": 120,        # seconds — LLM API call timeout; vision payloads need generous timeout
            "extra_body": {},      # OpenAI-compatible provider-specific request fields
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
            "download_timeout": 30,  # seconds — image HTTP download timeout; increase for slow connections
        },
        "web_extract": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 360,        # seconds (6min) — per-attempt LLM summarization timeout; increase for slow local models
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        "compression": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 120,        # seconds — compression summarises large contexts; increase for local models
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Note: session_search no longer uses an auxiliary LLM (PR #27590 —
        # single-shape tool returns DB content directly). The old
        # ``auxiliary.session_search.*`` block was removed here. Existing
        # values in user config.yaml files are harmless leftovers and ignored.
        "skills_hub": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        "approval": {
            "provider": "auto",
            "model": "",           # fast/cheap model recommended (e.g. gemini-flash, haiku)
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        "mcp": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        "title_generation": {
            "enabled": True,
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
            "language": "",
        },
        "memory_query_rewrite": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 8,
            "extra_body": {},
        },
        "tts_audio_tags": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Triage specifier — flesh out a rough one-liner in the Kanban
        # Triage column into a concrete spec, then promote it to ``todo``.
        # Invoked by ``hermes kanban specify`` (single id or --all). Set a
        # cheap, capable model here (gemini-flash works well); the main
        # model is overkill for short spec expansion.
        "triage_specifier": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 120,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Kanban decomposer — decomposes a triage task into a graph of
        # child tasks routed to specialist profiles by description.
        # Invoked by ``hermes kanban decompose`` and the kanban
        # auto-decompose dispatcher tick. Returns a JSON task graph;
        # uses more tokens than the specifier so allow more headroom.
        "kanban_decomposer": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 180,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Profile describer — auto-generates a 1-2 sentence description
        # of what a profile is good at. Invoked by
        # ``hermes profile describe <name> --auto`` and the dashboard's
        # auto-generate button. Short, cheap call.
        "profile_describer": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 60,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Goal judge — evaluates whether a /goal run's latest response
        # satisfies the goal/contract, and drafts goal contracts. Short
        # structured-JSON calls; a fast cheap model is fine.
        "goal_judge": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 60,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Curator — skill-usage review fork. Timeout is generous because the
        # review pass can take several minutes on reasoning models (umbrella
        # building over hundreds of candidate skills). "auto" = use main chat
        # model; override via `hermes model` → auxiliary → Curator to route
        # to a cheaper aux model (e.g. openrouter google/gemini-3-flash-preview).
        "curator": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 600,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Monitor — urgency/importance classifier used by the important-mail
        # monitor catalog automation (cron/scripts/classify_items.py). Scores
        # candidate items 0-10 against the user's criteria so only above-
        # threshold items get delivered. "auto" = main chat model; override to
        # a cheap fast model (e.g. openrouter google/gemini-3-flash-preview,
        # haiku) since per-item scoring is high-volume and a small model is fine.
        "monitor": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 60,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        # Background review — the post-turn self-improvement fork that decides
        # whether to save a memory / patch a skill. "auto" (default) = run on
        # the main chat model, replaying the full conversation, which is already
        # warm in the prompt cache (cheap cache reads) — unchanged, optimal.
        # Set provider/model to a cheaper model (e.g. openrouter
        # google/gemini-3-flash-preview) to run the review there for ~3-5x lower
        # cost. A different model can't reuse the main prompt cache anyway, so
        # the fork automatically replays a compact digest instead of the full
        # transcript when routed (minimises the cold-write). Same model = full
        # replay; different model = digest. Quality holds (memory capture
        # identical, skill near-identical in benchmarks).
        "background_review": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 120,
            "extra_body": {},
            "reasoning_effort": "",  # per-task thinking level: none|minimal|low|medium|high|xhigh|max|ultra (empty = provider default)
        },
        "moa_reference": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 900,
            "extra_body": {},
            # NOTE: no reasoning_effort here by design — MoA reasoning depth is
            # configured PER SLOT in the MoA preset (moa.presets.<name>.
            # reference_models[].reasoning_effort / aggregator.reasoning_effort),
            # not at the auxiliary-task level.
        },
        "moa_aggregator": {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 900,
            "extra_body": {},
            # NOTE: no reasoning_effort here by design — see moa_reference above.
        },
    },
    
    "display": {
        "compact": False,
        "personality": "",
        "resume_display": "full",
        # Recap tuning for /resume and startup resume. The defaults match the
        # historical hardcoded values; expose them as config so power users can
        # widen or tighten the snapshot to taste.
        "resume_exchanges": 10,            # max user+assistant pairs to show
        "resume_max_user_chars": 300,      # truncate user message text
        "resume_max_assistant_chars": 200, # truncate non-last assistant text
        "resume_max_assistant_lines": 3,   # truncate non-last assistant lines
        # When True (default), assistant entries that are *only* tool calls
        # (no visible text) are skipped in the recap. This prevents the recap
        # from being dominated by `[2 tool calls: terminal, read_file]` lines
        # when an exchange was tool-heavy. Set False to restore the legacy
        # behavior of showing tool-call summaries inline.
        "resume_skip_tool_only": True,
        "busy_input_mode": "interrupt",  # interrupt | queue | steer
        # When busy_input_mode="steer", suppress only the visible
        # "Steered into current run" confirmation bubble by setting this false.
        # The mid-turn steering itself still happens.
        "busy_steer_ack_enabled": True,
        # Which interface bare `hermes` (and `hermes chat`) launches by default:
        #   "cli" — the classic prompt_toolkit REPL (default, preserves prior behavior)
        #   "tui" — the modern Ink TUI (same as passing `--tui`)
        # Explicit flags always win over this setting: `--cli` forces the classic
        # REPL and `--tui` (or HERMES_TUI=1) forces the TUI regardless of config.
        "interface": "cli",
        # When true, `hermes --tui` auto-resumes the most recent human-
        # facing session on launch instead of forging a fresh one.
        # Mirrors `hermes -c` muscle memory.  Default off so existing
        # users aren't surprised.  HERMES_TUI_RESUME=<id> always wins.
        "tui_auto_resume_recent": False,
        # When true (default), `hermes --tui` drops a one-time hint
        # ("subagents working · /agents to watch live") the first time a turn
        # starts delegating, nudging the user toward the live spawn-tree
        # dashboard. Set false to suppress the hint.
        "tui_agents_nudge": True,
        "bell_on_complete": False,
        # Stream the model's reasoning/thinking live before the response.
        # Default ON: on thinking models the reasoning phase can run tens of
        # seconds, and with this off the user stares at a spinner the whole
        # time even though tokens are streaming. Set false for quiet output.
        "show_reasoning": True,
        # When reasoning display is on, the post-response "Reasoning" recap box
        # collapses long thinking to the first 10 lines. Set true to print the
        # complete thinking text uncollapsed (live streaming is always full).
        "reasoning_full": False,
        # Background self-improvement review notifications surfaced in chat.
        #   "off"     — no chat notification (the review still runs and writes)
        #   "on"      — generic "💾 Memory updated" line (default)
        #   "verbose" — include a compact content preview of what changed
        # Per-platform overrides via display.platforms.<platform>.memory_notifications.
        "memory_notifications": "on",
        "streaming": False,
        "timestamps": False,      # Show timestamp on user and assistant labels
        "timestamp_format": "%H:%M",  # strftime format for timestamps (e.g. "%b-%d %H:%M")
        "final_response_markdown": "strip",  # render | strip | raw
        # Preserve recent classic CLI output across Ctrl+L, /redraw, and
        # terminal resize full-screen clears. Disable if a terminal emulator
        # behaves badly with replayed scrollback.
        "persistent_output": True,
        "persistent_output_max_lines": 200,
        # Print a one-line summary of resolved modal prompts (approval /
        # clarify) into scrollback so the question and decision survive the
        # panel repaint. Set false to keep scrollback untouched.
        "persist_prompts": True,
        "inline_diffs": True,     # Show inline diff previews for write actions (write_file, patch, skill_manage)
        # File-mutation verifier footer.  When true (default), the agent
        # appends a one-line advisory to its final response whenever a
        # write_file / patch call failed during the turn and was never
        # superseded by a successful write to the same path.  This catches
        # the "batch of parallel patches, half fail, model claims success"
        # class of over-claim that otherwise forces users to run
        # `git status` to verify edits landed.  Set false to suppress.
        "file_mutation_verifier": True,
        # Nous credits status-bar notices (usage bands, grant-spent, depleted /
        # restored).  When false, no credits notices are emitted — balance data
        # is still captured and /usage keeps working.  Off switch for sub +
        # top-up users who find the gauge noisy.
        "credits_notices": True,
        # Turn-completion explainer.  When true (default), the agent appends a
        # one-line explanation to its final response whenever a turn ends
        # abnormally with no usable reply — empty content after retries, a
        # partial/truncated stream, a still-pending tool result, or an
        # iteration/budget limit.  Replaces the bare "(empty)" sentinel so the
        # failure isn't silent from the UI's perspective.  Set false to suppress.
        "turn_completion_explainer": True,
        "show_cost": False,       # Show $ cost in the status bar (off by default)
        # Show a color-coded battery read-out as the first status-bar element in
        # the CLI/TUI (off by default). No-op on machines without a battery.
        "battery": False,
        # Focus view (/focus): display-only reduced-output mode. When true the
        # CLI/TUI pins tool_progress to "off" (reusing the existing suppression
        # path), reports a per-turn hidden-line count with a recovery hint, and
        # pins a "focus" segment in the status bar. focus_saved_tool_progress
        # holds the mode /focus off restores. Never affects what is sent to the
        # model — see hermes_cli/focus_view.py.
        "focus_view": False,
        "focus_saved_tool_progress": "all",
        "skin": "default",
        # UI language for static user-facing messages (approval prompts, a
        # handful of gateway slash-command replies).  Does NOT affect agent
        # responses, log lines, tool outputs, or slash-command descriptions.
        # Supported: en, zh, ja, de, es, fr, tr, uk.  Unknown values fall back to en.
        "language": "en",
        # TUI busy indicator style: kaomoji (default), emoji, unicode (braille
        # spinner), or ascii.  Live-swappable via `/indicator <style>`.
        "tui_status_indicator": "kaomoji",
        # Seconds between prompt_toolkit redraws in the classic CLI when idle.
        # Default 1.0 keeps the wall-clock status-bar read-outs (idle-since-
        # last-turn) ticking and keeps the bottom chrome alive during idle —
        # without it prompt_toolkit stops repainting the status bar after a
        # turn and it can go stale/disappear (#45592).
        # Set 0 to disable the background refresh if it fights terminal
        # auto-scroll in non-fullscreen mode on some emulators (#48309).
        "cli_refresh_interval": 1.0,
        "user_message_preview": {  # CLI: how many submitted user-message lines to echo back in scrollback
            "first_lines": 2,
            "last_lines": 2,
        },
        "interim_assistant_messages": True,  # Gateway: send natural mid-turn assistant status messages. Desktop: keep mid-turn narration between tool calls instead of collapsing to the final message.
        # Codex Responses models narrate progress in a dedicated commentary
        # channel. When true (default), completed commentary messages are
        # delivered as visible mid-turn updates via the interim message path.
        # When false, commentary falls back to the reasoning channel and is
        # only visible when show_reasoning is enabled.
        "show_commentary": True,
        "tool_progress_command": False,  # Enable /verbose command in messaging gateway
        "tool_progress_overrides": {},  # DEPRECATED — use display.platforms instead
        "tool_preview_length": 0,  # Max chars for tool call previews (0 = no limit, show full paths/commands)
        # Human-phrased tool status labels for built-in tools: "Searching the
        # web for ...", "Reading <file>", "Browsing <url>" instead of the raw
        # tool name. Applies to CLI spinner + gateway/desktop tool-progress.
        # Custom/plugin/MCP tools always fall back to the raw preview.
        "friendly_tool_labels": True,
        # CLI-only post-turn accounting line printed after each interactive turn:
        # "⋯ 12.4s · edited 2 files +18 -3 · read 4 files · ran 3 commands".
        # Observed from the tool-progress feed the CLI already receives; never
        # printed in quiet/non-interactive paths or in gateway/messaging
        # surfaces (those have their own runtime footer).
        "turn_summary": True,
        # CLI-only: append cumulative turn output tokens to the live spinner
        # timer ("⚡ Reading file  ( 2.3s · ↓ 1.2k tok)"). Updates as each API
        # call in the turn reports usage.
        "spinner_token_flow": True,
        # How gateway tool-progress is grouped on platforms that support message
        # editing: "accumulate" (default) edits one bubble in place; "separate"
        # sends one message per tool (the pre-v0.9 behavior, noisier). Only
        # applies where tool_progress is already enabled. Per-platform override
        # via display.platforms.<platform>.tool_progress_grouping.
        "tool_progress_grouping": "accumulate",
        # Optional custom phrases for generic long-running status messages.
        # Built-in defaults live in gateway/assets/status_phrases.yaml. Users
        # can set `path`/`paths` to HERMES_HOME-relative YAML files/directories
        # (or rely on conventional status_phrases.yaml / status_phrases/*.yaml).
        # Keys: status, generic. Use
        # mode: "append" (default) to add phrases, or "replace" to fully
        # replace configured surfaces. Per-platform overrides live under
        # display.platforms.<platform>.status_phrases.
        "status_phrases": {},
        # How a reasoning/thinking summary renders when show_reasoning is on.
        # "code" (default) = 💭 fenced code block; "blockquote" = "> " lines;
        # "subtext" = "-# " lines (Discord small grey metadata text). Discord
        # defaults to "subtext"; override per-platform via
        # display.platforms.<platform>.reasoning_style.
        "reasoning_style": "code",
        # Auto-delete system-notice replies (e.g. "✨ New session started!",
        # "♻ Restarting gateway…", "⚡ Stopped…") after N seconds on platforms
        # that support message deletion (currently Telegram; other platforms
        # ignore and leave the message in place).  Only affects slash-command
        # replies wrapped with gateway.platforms.base.EphemeralReply — agent
        # responses and content messages are never touched.  Default 0
        # (disabled) preserves prior behavior.
        "ephemeral_system_ttl": 0,
        # Per-platform display/streaming overrides. Each key is a gateway
        # platform ("telegram", "discord", "slack", …) mapping to a dict of
        # display settings that override the global value for that platform
        # only. A setting left unset here falls through to the global default.
        #
        # Shipped defaults encode the streaming experience that works best
        # per platform:
        #   - Telegram has native animated draft streaming (sendMessageDraft),
        #     which is smooth, so streaming is on by default there.
        #   - Discord and Slack only have edit-based streaming (repeated
        #     editMessage), which flickers and is noticeably jankier, so
        #     streaming is off by default for both.
        # These are gap-fillers: a user who explicitly sets, e.g.,
        # display.platforms.discord.streaming: true keeps their value
        # (config deep-merge has user values win over defaults). The global
        # streaming.enabled master switch still gates everything — these
        # per-platform flags only take effect once streaming is enabled.
        "platforms": {
            "telegram": {"streaming": True},
            "discord": {"streaming": False},
            "slack": {"streaming": False},
        },
        # Gateway runtime-metadata footer appended to the FINAL message of a turn
        # (disabled by default to keep replies minimal). When enabled, renders
        # e.g. `model · 68% · ~/projects/hermes`. Per-platform overrides go under
        # display.platforms.<platform>.runtime_footer.
        "runtime_footer": {
            "enabled": False,
            "fields": ["model", "context_pct", "cwd"],  # Order shown; drop any to hide
        },
        "copy_shortcut": "auto",  # "auto" (platform default) | "ctrl_c" | "ctrl_shift_c" | "disabled"
        # Petdex animated mascot (https://github.com/crafter-station/petdex).
        # A purely cosmetic sprite that reacts to agent activity across the
        # CLI, TUI, and desktop app. Manage with `hermes pets`. Disabled until
        # a pet is installed + selected (no effect on prompt caching — this is
        # a display concern only).
        "pet": {
            "enabled": False,
            # Active pet slug; resolved against installed pets in
            # get_hermes_home()/pets/. Empty → first installed pet.
            "slug": "",
            # Terminal render protocol for CLI/TUI:
            #   auto  — detect kitty/iTerm2/sixel, else unicode half-blocks
            #   kitty | iterm | sixel | unicode | off
            "render_mode": "auto",
            # Master size scalar (relative to native 192×208 frames). One knob
            # shrinks every surface: the desktop canvas scales its pixels by it
            # and the CLI/TUI derive their terminal column width from it. The
            # half-block fallback clamps to a legibility floor (it can't shrink
            # as far as true-pixel kitty/GUI without turning to mush).
            "scale": 0.33,
            # Hard override for terminal column width. 0 = auto (derive from
            # scale); set a positive int only to pin the half-block/kitty width
            # independently of scale.
            "unicode_cols": 0,
        },
    },

    # Web dashboard settings
    "dashboard": {
        "theme": "default",  # Dashboard visual theme: "default", "midnight", "ember", "mono", "cyberpunk", "rose"
        # Process-isolation rollout controls. Runtime reads these through the
        # raw config loader, so tui_gateway.server also owns explicit defaults.
        "turn_isolation": False,
        "compute_host_heartbeat_secs": 15,
        "compute_host_respawn_max": 3,
        # Hide the token/cost analytics surfaces (Analytics page, token bars and
        # cost figures on the Models page) by default.  The numbers shown there
        # are a local debug estimate: they only count successful main-agent
        # responses with a usable ``response.usage``, and silently exclude every
        # auxiliary call (context compression, title generation, vision,
        # session search, web extract, smart approval, MCP routing, plugin LLM
        # access) plus provider-side retries, fallback attempts, and any call
        # whose usage block didn't come back.  Cache writes are also missing
        # from the API response.  On models with heavy auxiliary traffic
        # (Kimi K2.6, MiniMax M2.7) the local total can be 10x-100x lower than
        # the provider bill, which is worse than hiding the numbers entirely
        # because they look precise enough to compare against the provider.
        # Set this to True to re-enable the surfaces with the understanding
        # that the numbers are a local lower-bound estimate, not billing.
        "show_token_analytics": False,
        # OAuth gate configuration (engaged when ``--host`` is set and
        # ``--insecure`` is not). The bundled Nous Portal plugin reads
        # both keys at startup; they are the canonical surface for these
        # settings. Each can be overridden by an environment variable —
        # ``HERMES_DASHBOARD_OAUTH_CLIENT_ID`` and
        # ``HERMES_DASHBOARD_PORTAL_URL`` respectively — and the env var
        # wins when set to a non-empty value. The override path is what
        # Fly.io's platform-secret injection uses to push the per-deploy
        # client_id at provisioning time without operators needing to
        # touch config.yaml. Local dev / non-Fly deploys can set either
        # surface; missing values fall through to the plugin's defaults
        # (no provider registered when ``client_id`` is empty;
        # ``portal_url`` defaults to https://portal.nousresearch.com).
        "oauth": {
            "client_id": "",  # agent:{instance_id} — Portal provisions this
            "portal_url": "",  # blank → use plugin default (production Portal)
        },
        # Username/password gate configuration — read by the bundled
        # ``dashboard_auth/basic`` plugin (a self-hosted "just put a
        # password on my dashboard" provider that needs no OAuth IDP).
        # The plugin registers a password provider when ``username`` plus
        # either ``password_hash`` (preferred — no plaintext at rest) or
        # ``password`` (plaintext, hashed in-memory at load) are set. Each
        # key is overridable by an env var
        # (``HERMES_DASHBOARD_BASIC_AUTH_USERNAME`` /
        # ``_PASSWORD_HASH`` / ``_PASSWORD`` / ``_SECRET`` /
        # ``_TTL_SECONDS``), env winning when non-empty. Leave ``username``
        # empty (the default) to keep the plugin a no-op — loopback /
        # ``--insecure`` operators and OAuth users are unaffected.
        #
        # ``secret`` is the HMAC key used to sign the stateless session
        # tokens this provider mints. When empty, a random per-process key
        # is generated — fine for a single process, but sessions then
        # don't survive a restart or span multiple workers. Set an
        # explicit ``secret`` (32+ random bytes, base64/hex/raw) for
        # stable multi-worker / restart-surviving sessions. Compute a
        # ``password_hash`` with
        # ``python -c "from plugins.dashboard_auth.basic import hash_password; print(hash_password('PW'))"``.
        "basic_auth": {
            "username": "",  # blank → plugin no-op (no password provider)
            "password_hash": "",  # scrypt$... (preferred — no plaintext at rest)
            "password": "",  # plaintext fallback (hashed in-memory at load)
            "secret": "",  # token-signing key; blank → random per-process
            "session_ttl_seconds": 0,  # 0 → plugin default (12h)
        },
        # Drain-control service-credential configuration — read by the
        # bundled ``dashboard_auth/drain`` plugin (the first consumer of the
        # generic non-interactive token-auth capability). The SECRET itself
        # is a credential and is NOT configured here: it is provisioned by
        # nous-account-service at deploy time via the
        # ``HERMES_DASHBOARD_DRAIN_SECRET`` env var (the .env-is-for-secrets
        # rule). These are the behavioural knobs only. The plugin is a no-op
        # unless that env var is set to a >=256-bit secret; a weak secret is
        # rejected at registration (fail-closed) and the drain endpoint stays
        # disabled. ``scope`` is the capability label attached to the verified
        # principal; ``min_secret_chars`` is the entropy bar (url-safe-b64
        # chars; 43 ~= 256 bits).
        "drain_auth": {
            "scope": "drain",
            "min_secret_chars": 43,
        },
        # Public URL override (env: ``HERMES_DASHBOARD_PUBLIC_URL``).
        # When set, this is the complete authority — scheme + host +
        # optional path prefix (e.g. ``https://example.com/hermes``) —
        # the OAuth ``redirect_uri`` is built from. Set this for deploys
        # behind reverse proxies that don't reliably forward
        # ``X-Forwarded-Host`` / ``X-Forwarded-Proto`` / ``X-Forwarded-Prefix``
        # (manual nginx setups, on-prem ingresses, custom-domain Fly
        # deploys without proper proxy headers). When set,
        # ``X-Forwarded-Prefix`` is IGNORED on the OAuth path because
        # the operator has declared the public URL — we no longer need
        # to guess from proxy headers, and stacking the prefix on top
        # would double-prefix the common case where the prefix is
        # already baked into ``public_url``. Leave empty to use the
        # existing proxy-header reconstruction (the default).
        #
        # Validation: rejects values without ``http(s)://`` scheme or
        # without a host, and any string containing quote / angle /
        # whitespace / control characters. A malformed value silently
        # falls through to request reconstruction rather than breaking
        # the login flow.
        "public_url": "",
    },

    # Privacy settings
    "privacy": {
        "redact_pii": False,  # When True, hash user IDs and strip phone numbers from LLM context
    },
    
    # Text-to-speech configuration
    # Each provider supports an optional `max_text_length:` override for the
    # per-request input-character cap. Omit it to use the provider's documented
    # limit (OpenAI 4096, xAI 15000, MiniMax 10000, ElevenLabs 5k-40k model-aware,
    # Gemini 32000, Edge 5000, Mistral 4000, NeuTTS/KittenTTS 2000).
    "tts": {
        # Set explicitly to pin a backend:
        # "edge" (free) | "elevenlabs" (premium) | "openai" | "xai" | "minimax" | "mistral" | "gemini" | "deepinfra" | "neutts" (local) | "kittentts" (local) | "piper" (local)
        "provider": "edge",
        "edge": {
            "voice": "en-US-AriaNeural",
            # Popular: AriaNeural, JennyNeural, AndrewNeural, BrianNeural, SoniaNeural
        },
        "elevenlabs": {
            "voice_id": "pNInz6obpgDQGcFmaJgB",  # Adam
            "model_id": "eleven_multilingual_v2",
        },
        "openai": {
            "model": "gpt-4o-mini-tts",
            "voice": "alloy",
            # Voices: alloy, ash, ballad, cedar, coral, echo, fable, marin,
            # nova, onyx, sage, shimmer, verse (gpt-4o-mini-tts; the tts-1
            # era stopped at alloy/echo/fable/onyx/nova/shimmer)
        },
        "gemini": {
            "model": "gemini-2.5-flash-preview-tts",
            "voice": "Kore",
            # When true, Gemini 3.1 TTS uses a hidden auxiliary-model rewrite
            # pass to insert freeform square-bracket audio tags into the TTS
            # script. Visible chat replies are unchanged.
            "audio_tags": False,
            # Optional local Markdown/text file with Gemini TTS performance
            # direction. It may include AUDIO PROFILE, SCENE, DIRECTOR'S NOTES,
            # SAMPLE CONTEXT, and either a `{transcript}` placeholder or no
            # transcript section; Hermes appends the live transcript when absent.
            "persona_prompt_file": "",
        },
        "xai": {
            "voice_id": "eve",  # or custom voice ID — see https://docs.x.ai/developers/model-capabilities/audio/custom-voices
            "language": "en",  # BCP-47 code ("en", "pt-BR") or "auto"
            "speed": 1.0,  # 0.7–1.5, playback speed
            "auto_speech_tags": False,  # insert expressive audio tags via LLM rewrite
            "optimize_streaming_latency": 0,  # 0–2, trades quality for lower latency
            "sample_rate": 24000,  # 22050 / 24000 / 44100 / 48000
            "bit_rate": 128000,  # MP3 bitrate; only applies when codec=mp3
        },
        "mistral": {
            "model": "voxtral-mini-tts-2603",
            "voice_id": "c69964a6-ab8b-4f8a-9465-ec0925096ec8",  # Paul - Neutral
        },
        "minimax": {
            "model": "speech-02-hd",
            "voice_id": "English_expressive_narrator",
        },
        "kittentts": {
            "model": "KittenML/kitten-tts-nano-0.8-int8",  # nano 25MB; micro 41MB; mini 80MB
            "voice": "Jasper",
        },
        "neutts": {
            "ref_audio": "",  # Path to reference voice audio (empty = bundled default)
            "ref_text": "",   # Path to reference voice transcript (empty = bundled default)
            "model": "neuphonic/neutts-air-q4-gguf",  # HuggingFace model repo
            "device": "cpu",  # cpu, cuda, or mps
        },
        "piper": {
            # Voice name (e.g. "en_US-lessac-medium") downloaded on first
            # use, OR an absolute path to a pre-downloaded .onnx file.
            # Full voice list: https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md
            "voice": "en_US-lessac-medium",
            # "voices_dir": "",        # Override voice cache dir; default = ~/.hermes/cache/piper-voices/
            # "use_cuda": False,       # Requires onnxruntime-gpu
            # "length_scale": 1.0,     # 2.0 = twice as slow
            # "noise_scale": 0.667,
            # "noise_w_scale": 0.8,
            # "volume": 1.0,
            # "normalize_audio": True,
        },
        "deepinfra": {
            "model": "",  # empty = first tts-tagged model from the live catalog
            "voice": "default",
            # "base_url": "",  # override DEEPINFRA_BASE_URL for TTS only
        },
    },

    "stt": {
        "enabled": True,
        # When true, gateway voice messages are transcribed for the agent and
        # the raw transcript is also echoed back to the user as a 🎙️ message.
        # Set false to keep STT for the agent while suppressing that user-facing echo.
        "echo_transcripts": True,
        "provider": "local",  # "local" (free, faster-whisper) | "groq" | "openai" (Whisper API) | "mistral" (Voxtral Transcribe) | "elevenlabs" (Scribe) | "deepinfra"
        # Global language hint applied to EVERY provider unless a per-provider
        # language overrides it. Defaults to "en" — Whisper auto-detection
        # frequently misidentifies short/accented clips, which reads as
        # "STT transcribed the wrong language". Set to "" to restore
        # auto-detect, or to your language code ("es", "zh", "uk", ...).
        "language": "en",
        "local": {
            "model": "base",  # tiny, base, small, medium, large-v3
            "language": "",  # auto-detect by default; set to "en", "es", "fr", etc. to force
            "initial_prompt": "",
            # Anti-hallucination hardening (faster-whisper decodes junk tokens
            # from silence/noise without these):
            "vad": True,  # Silero VAD filter — silence never reaches whisper. false = old raw behavior (music/ambient).
            "vad_min_silence_ms": 500,  # min silence (ms) that splits speech chunks when vad is on
            "no_speech_prob_threshold": 0.6,  # drop a segment only if no_speech_prob is ABOVE this...
            "logprob_threshold": -1.0,  # ...AND its avg_logprob is BELOW this (both must hit)
        },
        "groq": {
            "model": "whisper-large-v3-turbo",  # whisper-large-v3, whisper-large-v3-turbo, distil-whisper-large-v3-en
            "language": "",  # auto-detect by default; set to "en", "es", "fr", etc. to force
        },
        "openai": {
            "model": "whisper-1",  # whisper-1, gpt-4o-mini-transcribe, gpt-4o-transcribe, gpt-transcribe
            "language": "",  # auto-detect by default; set to "en", "es", "fr", etc. to force
        },
        "mistral": {
            "model": "voxtral-mini-latest",  # voxtral-mini-latest, voxtral-mini-2602
            "language": "",  # auto-detect by default; set to "en", "es", "fr", etc. to force
        },
        "xai": {
            "language": "",  # auto-detect by default; set to "en", "es", "fr", etc. to force
        },
        "elevenlabs": {
            "model_id": "scribe_v2",  # scribe_v2, scribe_v1
            "language_code": "",  # auto-detect by default; set to "eng", "spa", "fra", etc. to force
            "tag_audio_events": False,
            "diarize": False,
        },
        "deepinfra": {
            "model": "",  # empty = first stt-tagged model from the live catalog
            # "base_url": "",  # override DEEPINFRA_BASE_URL for STT only
        },
    },

    "voice": {
        "record_key": "ctrl+b",
        "max_recording_seconds": 120,
        "auto_tts": False,
        "beep_enabled": True,         # Play record start/stop beeps in CLI voice mode
        "beep_volume": 0.3,           # Beep amplitude multiplier (0.0-1.0, default keeps prior hardcoded value)
        "silence_threshold": 200,     # RMS below this = silence (0-32767)
        "silence_duration": 3.0,      # Seconds of silence before auto-stop
        "barge_in": True,             # Stop TTS playback when the user starts talking
        # Saying EXACTLY one of these phrases (and nothing else) ends the
        # voice chat instead of being sent to the agent. Case-insensitive,
        # surrounding punctuation ignored. Set [] to disable.
        "stop_phrases": ["stop"],
    },

    # "Hey Hermes" hands-free wake word. Always-on, on-device hotword
    # detection that starts a fresh voice session — the "Hey Siri" pattern.
    # Off by default; toggle with /wake or `wake_word.enabled: true`.
    "wake_word": {
        "enabled": False,
        "surface": "auto",            # eligible surface: "auto" (first claimant) | "cli" | "tui" | "gui"
        "provider": "openwakeword",   # "openwakeword" (free, local) | "sherpa" (free, ANY phrase, no training) | "porcupine" (premium; needs PORCUPINE_ACCESS_KEY)
        "phrase": "hey hermes",       # for "sherpa" this IS the detected phrase (any text works); for other engines it's a cosmetic label — detection is keyed by the model/keyword below
        "sensitivity": 0.6,           # 0.0-1.0 detection threshold, consistent across engines (higher = stricter, fewer false triggers)
        "confirmation_frames": 3,     # openWakeWord only: consecutive over-threshold frames required to fire (higher = fewer false triggers on ambient speech, slightly more latency; 1 = old single-frame behavior)
        "start_new_session": True,    # start a fresh session on wake vs. continue the current one
        "profile_routing": True,      # sherpa only: also listen for every wake-enabled profile's phrase and route the wake to the matching profile
        "openwakeword": {
            # "hey_hermes" (the bundled, works-out-of-the-box default) OR a
            # built-in openWakeWord name ("hey_jarvis", "alexa", "hey_mycroft",
            # ...) OR a path to a custom .onnx/.tflite model for another phrase.
            # See the wake-word docs for the custom-model training guide.
            "model": "hey_hermes",
            # "" (auto — tflite on macOS ARM64, onnx elsewhere) | "onnx" | "tflite".
            # openWakeWord's onnx backend scores near-zero on macOS ARM64
            # (dscripka/openWakeWord#336), so auto avoids a listener that arms
            # but never fires. Set explicitly only to override that choice.
            "inference_framework": "",
        },
        "sherpa": {
            # Optional path to a sherpa-onnx KWS model directory. Empty =
            # auto-download the small English zipformer model on first use.
            "model_dir": "",
        },
        "porcupine": {
            # Built-in keyword ("jarvis", "computer", "bumblebee", ...) or a path
            # to a custom .ppn from the Picovoice Console.
            "keyword": "jarvis",
        },
    },
    
    "human_delay": {
        "mode": "off",
        "min_ms": 800,
        "max_ms": 2500,
    },
    
    # Context engine -- controls how the context window is managed when
    # approaching the model's token limit.
    # "compressor" = built-in lossy summarization (default).
    # Set to a plugin name to activate an alternative engine (e.g. "lcm"
    # for Lossless Context Management).  The engine must be installed as
    # a plugin in plugins/context_engine/<name>/ or ~/.hermes/plugins/.
    "context": {
        "engine": "compressor",
    },

    # Persistent memory -- bounded curated memory injected into system prompt
    "memory": {
        "memory_enabled": True,
        "user_profile_enabled": True,
        # Approval gate for memory writes (add/replace/remove), applied to BOTH
        # foreground agent turns and the background self-improvement review fork
        # (the source of unprompted "wrong assumption" saves users reported).
        #   false (default) — write freely; the gate is off (pre-gate behaviour)
        #   true            — require approval: foreground writes prompt inline
        #                     (entries are small enough to review in a chat
        #                     bubble); background-review writes are staged
        #                     instead of committed (a daemon thread cannot block
        #                     on a prompt). Review staged entries with
        #                     /memory pending, /memory approve <id>,
        #                     /memory reject <id>.
        # To disable memory entirely, use memory_enabled: false instead.
        "write_approval": False,
        "memory_char_limit": 2200,   # ~800 tokens at 2.75 chars/token
        "user_char_limit": 1375,     # ~500 tokens at 2.75 chars/token
        # External memory provider plugin (empty = built-in only).
        # Set to a provider name to activate: "openviking", "mem0",
        # "hindsight", "holographic", "retaindb", "byterover".
        # Only ONE external provider is allowed at a time.
        "provider": "",
    },

    # Subagent delegation — override the provider:model used by delegate_task
    # so child agents can run on a different (cheaper/faster) provider and model.
    # Uses the same runtime provider resolution as CLI/gateway startup, so all
    # configured providers (OpenRouter, Nous, Z.ai, Kimi, etc.) are supported.
    "delegation": {
        "model": "",       # e.g. "google/gemini-3-flash-preview" (empty = inherit parent model)
        "provider": "",    # e.g. "openrouter" (empty = inherit parent provider + credentials)
        "base_url": "",    # direct OpenAI-compatible endpoint for subagents
        "api_key": "",     # API key for delegation.base_url (falls back to OPENAI_API_KEY)
        "api_mode": "",    # wire protocol for delegation.base_url: "chat_completions",
                           # "codex_responses", or "anthropic_messages". Empty = auto-detect
                           # from URL (e.g. /anthropic suffix → anthropic_messages). Set this
                           # explicitly for non-standard endpoints the heuristic can't detect.
        # When delegate_task narrows child toolsets explicitly, preserve any
        # MCP toolsets the parent already has enabled. On by default so
        # narrowing (e.g. toolsets=["web","browser"]) expresses "I want these
        # extras" without silently stripping MCP tools the parent already has.
        # Set to false for strict intersection.
        "inherit_mcp_toolsets": True,
        "max_iterations": 50,  # per-subagent iteration cap (each subagent gets its own budget,
                               # independent of the parent's max_iterations)
        # Subagent summaries return to the parent's context verbatim. A batch
        # fan-out (N children) returns N summaries at once, which can exceed
        # the parent's context window and trigger a compression/429 death
        # spiral. delegate_task sizes each summary against the parent's
        # remaining context headroom (split across the batch); when it must
        # trim, the full text is spilled to ~/.hermes/cache/delegation/
        # (mounted into remote backends) and the in-context summary becomes a
        # head+tail window plus a footer with the exact read_file offset to
        # page the omitted middle — the same convention web_extract uses for
        # large pages. Nothing is lost. max_summary_chars is a hard per-summary
        # character ceiling layered on top of that dynamic budget
        # (belt-and-suspenders for models that ignore the "be concise"
        # instruction). 0 disables the hard ceiling; the dynamic headroom
        # budget still applies.
        "max_summary_chars": 24000,

        "child_timeout_seconds": 0,  # optional wall-clock cap per child agent. 0 (default)
                                     # = no timeout: children fail only from real errors
                                     # (API, tools, iteration budget), never a delegation
                                     # stopwatch. Set a positive number of seconds
                                     # (floor 30s) to enforce a hard cap.
        "reasoning_effort": "",  # subagent effort: "ultra", "max", "xhigh", "high",
                                 # "medium", "low", "minimal", "none" (empty = inherit)
        "max_concurrent_children": 3,  # unified concurrency cap: max parallel children per batch
                                       # AND max concurrent background (background=true)
                                       # delegation units. New async dispatches beyond the cap
                                       # fall back to synchronous execution. Floor of 1, no ceiling.
                                       # (Replaces the deprecated max_async_children.)
        # Orchestrator role controls (see tools/delegate_tool.py:_get_max_spawn_depth
        # and _get_orchestrator_enabled).  Floored at 1, no upper ceiling —
        # raise deliberately, each level multiplies API cost.
        "max_spawn_depth": 1,        # depth (1 = flat [default], 2 = orchestrator→leaf, 3+ = deeper)
        "orchestrator_enabled": True,  # kill switch for role="orchestrator"
        # When a subagent hits a dangerous-command approval prompt, the parent's
        # prompt_toolkit TUI owns stdin — a thread-local input() call from the
        # subagent worker would deadlock the parent UI. To avoid the deadlock,
        # subagent threads ALWAYS resolve approvals non-interactively:
        #   false (default) → auto-deny with a logger.warning audit line (safe)
        #   true             → auto-approve "once" with a logger.warning audit line
        # Flip to true only if you trust delegated work to run dangerous cmds
        # without human review (cron pipelines, batch automation, etc.).
        "subagent_auto_approve": False,
    },

    # Ephemeral prefill messages file — JSON list of {role, content} dicts
    # injected at the start of every API call for few-shot priming.
    # Never saved to sessions, logs, or trajectories.
    "prefill_messages_file": "",

    # Goals — persistent cross-turn goals (Ralph-style loop).
    # After every turn, a lightweight judge call asks the auxiliary model
    # whether the active /goal is satisfied by the assistant's last
    # response. If not, Hermes feeds a continuation prompt back into the
    # same session and keeps working until the goal is done, the turn
    # budget is exhausted, or the user pauses/clears it. Judge failures
    # fail OPEN (continue) so a flaky judge never wedges progress — the
    # turn budget is the real backstop.
    "goals": {
        # Max continuation turns before Hermes auto-pauses the goal and
        # asks the user to /goal resume. Protects against judge false
        # negatives (goal actually done but judge says continue) and
        # unbounded model spend on fuzzy / unachievable goals.
        "max_turns": 20,
    },

    # Mixture of Agents — named presets used by /moa. A preset is an execution
    # mode around the main model, not a provider/model itself: references +
    # aggregator synthesize private guidance before each main-model iteration.
    "moa": {
        "default_preset": "default",
        "active_preset": "",
        # When true, every MoA turn that runs the reference fan-out writes the
        # FULL turn (each reference's exact input messages + output + usage/cost,
        # and the aggregator's exact input + output) to a JSONL file at
        # <hermes_home>/moa-traces/<session_id>.jsonl. Off by default — turn it
        # on to audit / improve MoA behavior from real runs. Set trace_dir to
        # override the output directory.
        "save_traces": False,
        "trace_dir": "",
        # Privacy redaction filter for advisor (reference) outputs. Advisors
        # can echo PII from the conversation (emails, formatted phone numbers)
        # and credential shapes into reference blocks, traces, and the
        # aggregator prompt. Modes ('' = off, the default):
        #   "display" — redact user-visible surfaces only (reference blocks
        #               shown in the UI + saved MoA trace records); the
        #               aggregator still sees raw advisor text.
        #   "full"    — additionally redact the advisor text injected into
        #               the aggregator prompt (issue #59959).
        "privacy_filter": "",
        "presets": {
            "default": {
                "reference_models": [
                    {"provider": "openai-codex", "model": "gpt-5.5"},
                    {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
                ],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                "max_tokens": 4096,
                "enabled": True,
            }
        },
    },

    # Skills — external skill directories for sharing skills across tools/agents.
    # Each path is expanded (~, ${VAR}) and resolved.  Read-only — skill creation
    # always goes to ~/.hermes/skills/.
    "skills": {
        "external_dirs": [],   # e.g. ["~/.agents/skills", "/shared/team-skills"]
        # Substitute ${HERMES_SKILL_DIR} and ${HERMES_SESSION_ID} in SKILL.md
        # content with the absolute skill directory and the active session id
        # before the agent sees it.  Lets skill authors reference bundled
        # scripts without the agent having to join paths.
        "template_vars": True,
        # Pre-execute inline shell snippets written as !`cmd` in SKILL.md
        # body.  Their stdout is inlined into the skill message before the
        # agent reads it, so skills can inject dynamic context (dates, git
        # state, detected tool versions, …).  Off by default because any
        # content from the skill author runs on the host without approval;
        # only enable for skill sources you trust.
        "inline_shell": False,
        # Timeout (seconds) for each !`cmd` snippet when inline_shell is on.
        "inline_shell_timeout": 10,
        # Run the keyword/pattern security scanner on skills the agent
        # writes via skill_manage (create/edit/patch).  Off by default
        # because the agent can already execute the same code paths via
        # terminal() with no gate, so the scan adds friction (blocks
        # skills that mention risky keywords in prose) without meaningful
        # security.  Turn on if you want the belt-and-suspenders — a
        # dangerous verdict will then surface as a tool error to the
        # agent, which can retry with the flagged content removed.
        # External hub installs (trusted/community sources) are always
        # scanned regardless of this setting.
        "guard_agent_created": False,
        # Approval gate for skill_manage (create/edit/patch/write_file/delete/
        # remove_file), applied to BOTH foreground agent turns and the
        # background self-improvement review fork.
        #   false (default) — write freely; the gate is off (pre-gate behaviour)
        #   true            — require approval: stage the write for review
        #                     instead of committing (a SKILL.md is too large to
        #                     review inline, so skills always stage rather than
        #                     prompt). List with /skills pending, inspect with
        #                     /skills diff <id> (full diff — CLI/dashboard/file,
        #                     never crammed into a chat bubble), apply with
        #                     /skills approve <id> or drop with /skills reject <id>.
        "write_approval": False,
    },

    # Curator — background skill maintenance.
    #
    # Periodically reviews AGENT-CREATED skills (never bundled or
    # hub-installed) and keeps the collection tidy: marks long-unused skills
    # as stale, archives genuinely obsolete ones (archive only, never
    # deletes), and spawns a forked aux-model agent to consolidate overlaps
    # and patch drift. Runs inactivity-triggered from session start — no
    # cron daemon.
    #
    # See `hermes curator status` for the last run summary.
    "curator": {
        "enabled": True,
        # How long to wait between curator runs (hours).  Default: 7 days.
        "interval_hours": 24 * 7,
        # Only run when the agent has been idle at least this long (hours).
        "min_idle_hours": 2,
        # Mark a skill as "stale" after this many days without use.
        "stale_after_days": 30,
        # Archive a skill (move to skills/.archive/) after this many days
        # without use. Archived skills are recoverable — no auto-deletion.
        "archive_after_days": 90,
        # Run the LLM consolidation (umbrella-building) pass. OFF by default.
        # When off, a curator run does ONLY the deterministic inactivity prune
        # (mark stale / archive long-unused skills) and skips the forked
        # aux-model review entirely — no umbrella-building, no aux-model cost.
        # Set to true to opt back into merging overlapping skills into
        # class-level umbrellas. `hermes curator run --consolidate` overrides
        # this for a single invocation.
        "consolidate": False,
        # Also prune (archive) bundled built-in skills after the inactivity
        # period, not just agent-created ones. ON by default. Built-ins are
        # normally restored on every `hermes update`, so pruning them only
        # sticks because a suppression list tells the re-seeder to leave them
        # archived. Hub-installed skills are NEVER pruned here — they have an
        # external upstream owner. Built-ins accrue usage telemetry and their
        # inactivity clock starts the first time the curator sees them, so a
        # long-unused built-in is archived only after archive_after_days of
        # genuine non-use (never a mass-prune on the first run). Set to false
        # to keep all bundled built-ins permanently.
        "prune_builtins": True,
        # Pre-run backup: before every real curator pass (dry-run is
        # skipped), snapshot ~/.hermes/skills/ into
        # ~/.hermes/skills/.curator_backups/<utc-iso>/skills.tar.gz so the
        # user can roll back with `hermes curator rollback`.
        "backup": {
            "enabled": True,
            "keep": 5,  # retain last N regular snapshots
        },
    },

    # Honcho AI-native memory -- reads ~/.honcho/config.json as single source of truth.
    # This section is only needed for hermes-specific overrides; everything else
    # (apiKey, workspace, peerName, sessions, enabled) comes from the global config.
    "honcho": {},

    # IANA timezone (e.g. "Asia/Kolkata", "America/New_York").
    # Empty string means use server-local time.
    "timezone": "",

    # Slack platform settings (gateway mode)
    "slack": {
        "require_mention": True,       # Require @mention to respond in channels
        "free_response_channels": "",  # Comma-separated channel IDs where bot responds without mention
        "allowed_channels": "",        # If set, bot ONLY responds in these channel IDs (whitelist)
        # Channel IDs where @mention is ALWAYS required, even when
        # require_mention is false globally (per-channel force-mention override).
        "require_mention_channels": "",
        # Ignore a channel/thread message addressed to another user (first token
        # @mentions someone other than the bot) unless the bot is also mentioned.
        # Opt-in; default off keeps existing behaviour. Env: SLACK_IGNORE_OTHER_USER_MENTIONS.
        "ignore_other_user_mentions": False,
        # If True, require @mention in Slack thread replies too.
        "thread_require_mention": False,
        "channel_prompts": {},         # Per-channel ephemeral system prompts
    },

    # Discord platform settings (gateway mode)
    "discord": {
        "require_mention": True,       # Require @mention to respond in server channels
        "free_response_channels": "",  # Comma-separated channel IDs where bot responds without mention
        "allowed_channels": "",        # If set, bot ONLY responds in these channel IDs (whitelist)
        "auto_thread": True,           # Auto-create threads on @mention in channels (like Slack)
        "thread_require_mention": False,  # If True, require @mention in threads too (multi-bot threads)
        "bots_require_inline_mention": False,  # Multi-bot rooms: if True, another bot must type @thisbot in its message to trigger a reply; a Discord reply/quote alone won't. Prevents two bots auto-replying to each other forever. Does not affect humans.
        "history_backfill": True,         # If True, prepend recent channel scrollback when bot is triggered (recovers messages missed while require_mention gated them out)
        "history_backfill_limit": 50,     # Max number of recent messages to scan when assembling the backfill block
        "missed_message_backfill": {
            "enabled": False,             # Replay missed Discord messages after reconnect/startup
            "channels": "",               # Comma-separated channel IDs; empty uses free_response_channels
            "window_seconds": 21600,      # Only inspect messages from the last 6 hours
            "limit": 100,                 # Global cap on messages scanned per reconnect
            "max_dispatches": 10,         # Cap on recovered messages dispatched per reconnect
        },
        "reactions": True,             # Add 👀/✅/❌ reactions to messages during processing
        # Discord Gateway transport health. These settings inspect the active
        # WebSocket's ready/open/heartbeat state; they never use Discord REST as
        # proof that Gateway events are still arriving. Set any value to 0 to
        # disable this compatibility-safe probe during a rollback.
        "websocket_liveness_interval_seconds": 15,
        "websocket_liveness_failure_threshold": 2,
        "websocket_heartbeat_ack_max_age_seconds": 60,
        "websocket_max_latency_seconds": 30,
        "channel_prompts": {},         # Per-channel ephemeral system prompts (forum parents apply to child threads)
        # Opt-in DM role-based auth (#12136). By default, DISCORD_ALLOWED_ROLES
        # authorizes only guild messages in the role's own guild — DMs require
        # DISCORD_ALLOWED_USERS. Set dm_role_auth_guild to a guild ID to also
        # authorize DMs from members of that one trusted guild holding the
        # allowed role. Unset / empty / 0 = secure default (DM role-auth off).
        "dm_role_auth_guild": "",
        # discord / discord_admin tools: restrict which actions the agent may call.
        # Default (empty) = all actions allowed (subject to bot privileged intents).
        # Accepts comma-separated string ("list_guilds,list_channels,fetch_messages")
        # or YAML list. Unknown names are dropped with a warning at load time.
        # Actions: list_guilds, server_info, list_channels, channel_info,
        # list_roles, member_info, search_members, fetch_messages, list_pins,
        # pin_message, unpin_message, create_thread, add_role, remove_role.
        "server_actions": "",
        # DEPRECATED / no-op. Any uploaded file is now always cached and
        # surfaced to the agent regardless of file type — authorization to
        # message the agent is the gate, not the extension. Kept so existing
        # configs that set it do not error. Env override:
        # DISCORD_ALLOW_ANY_ATTACHMENT.
        "allow_any_attachment": False,
        # Maximum bytes per attachment the gateway will cache. The whole file
        # is held in memory while being written, so unlimited uploads carry a
        # real memory cost. Default 32 MiB matches the historical hardcoded
        # cap. Set to 0 for no cap. Env override: DISCORD_MAX_ATTACHMENT_BYTES.
        "max_attachment_bytes": 33554432,
        # When True, Discord approval prompts mention numeric allowed users so
        # owners notice approval requests in shared channels/threads. Env
        # override: DISCORD_APPROVAL_MENTIONS. Default false avoids surprise
        # pings.
        "approval_mentions": False,
        # Discord voice-channel inactivity timeout, in seconds. Set to 0 to
        # keep the bot in VC until an explicit `/voice leave` / disconnect.
        "voice_channel_inactivity_timeout_seconds": 300,
        # Minimum seconds to wait for a VC playback before force-stopping it.
        # The adapter also probes clip duration and extends this floor by a
        # padding window, so long TTS readbacks are not cut at exactly 120s.
        "voice_playback_timeout_seconds": 120,
        # Voice-channel audio effects (the continuous mixer). OFF by default.
        # When enabled, the bot installs a software mixer on the outgoing voice
        # stream so a low ambient "thinking" bed, verbal acknowledgements, and
        # TTS replies can OVERLAP (ducking the ambient under speech) instead of
        # stop-and-swap — the Grok-voice-mode feel. discord.py ships no mixer;
        # this is implemented in plugins/platforms/discord/voice_mixer.py.
        "voice_fx": {
            "enabled": False,         # master switch for the mixer subsystem
            "ambient_enabled": True,  # play the idle "thinking" bed while tools run
            "ambient_path": "",       # custom loop audio file; "" = synthesised pad
            "ambient_gain": 0.18,     # idle bed loudness, 0.0–1.0
            "duck_gain": 0.06,        # ambient loudness while speech plays
            "speech_gain": 1.0,       # TTS / ack loudness, 0.0–1.0
            "ack_enabled": True,      # speak a short phrase before the first tool call
            "ack_phrases": [          # picked at random; set [] to disable phrases
                "Let me look into that.",
                "One moment.",
                "Checking on that now.",
                "Give me a sec.",
                "On it.",
            ],
        },
    },

    # WhatsApp platform settings (gateway mode)
    "whatsapp": {
        # Reply prefix prepended to every outgoing WhatsApp message.
        # Default (None) uses the built-in "⚕ *Hermes Agent*" header.
        # Set to "" (empty string) to disable the header entirely.
        # Supports \n for newlines, e.g. "🤖 *My Bot*\n──────\n"
    },

    # Telegram platform settings (gateway mode)
    "telegram": {
        "reactions": False,            # Add 👀/✅/❌ reactions to messages during processing
        "channel_prompts": {},         # Per-chat/topic ephemeral system prompts (topics inherit from parent group)
        "allowed_chats": "",           # If set, bot ONLY responds in these group/supergroup chat IDs (whitelist)
        "extra": {
            "rich_messages": False,     # Bot API 10.1 rich messages (tables/task lists/details/math) render natively; set True to opt in. Default stays legacy MarkdownV2 because rich messages can be hard to copy as plain text in Telegram clients.
            "rich_drafts": False,       # Experimental Bot API 10.1 rich draft previews during Telegram DM streaming. Default off because Telegram Desktop/macOS can visually overlay rich draft frames until the chat redraws.
        },
    },

    # Mattermost platform settings (gateway mode)
    "mattermost": {
        "require_mention": True,       # Require @mention to respond in channels
        "free_response_channels": "",  # Comma-separated channel IDs where bot responds without mention
        "allowed_channels": "",        # If set, bot ONLY responds in these channel IDs (whitelist)
        "channel_prompts": {},         # Per-channel ephemeral system prompts
    },

    # Matrix platform settings (gateway mode)
    "matrix": {
        "require_mention": True,       # Require @mention to respond in rooms
        "free_response_rooms": "",     # Comma-separated room IDs where bot responds without mention
        "allowed_rooms": "",           # If set, bot ONLY responds in these room IDs (whitelist)
    },

    # Approval mode for dangerous commands:
    #   manual — always prompt the user
    #   smart  — use auxiliary LLM to auto-approve low-risk commands (default)
    #   off    — skip all approval prompts (equivalent to --yolo)
    #
    # cron_mode — what to do when a cron job hits a dangerous command:
    #   deny    — block the command and let the agent find another way (default, safe)
    #   approve — auto-approve all dangerous commands in cron jobs
    #
    # timeout — seconds to wait for the user's approve/deny before failing
    # closed (deny). Shared by the CLI prompt and gateway/messaging waits.
    # Messaging approvals arrive as a push notification the user may not see
    # immediately — 60s proved too tight on Telegram/Discord (the prompt
    # expired before the user reached their phone), so the default is 300.
    "approvals": {
        "mode": "smart",
        "timeout": 300,
        "cron_mode": "deny",
        # Operator-customizable policy text for smart approvals. When
        # non-empty, this is appended to the smart-approval guardian's
        # SYSTEM prompt (trusted channel) as additional rules — e.g.
        # "Always ESCALATE commands touching /etc" or "APPROVE docker
        # compose restarts under ~/deploys". Inspired by ChatGPT Work's
        # customizable auto-review guardian policy.
        "smart_policy": "",
        # Consecutive-denial circuit breaker for smart approvals: after this
        # many guardian DENY verdicts in a row within one session, the deny
        # message returned to the model escalates to a hard-stop instruction
        # (report to the user / ask for manual run or /approve) instead of a
        # plain "Do NOT retry". Any approval resets the count. 0 disables.
        # Inspired by ChatGPT Work's auto-review circuit breaker.
        "denial_breaker_threshold": 3,
        # User-defined deny rules: fnmatch globs matched against terminal
        # commands. A match blocks the command unconditionally — BEFORE the
        # --yolo / /yolo / mode=off bypass — making this the user-editable
        # counterpart to the code-shipped hardline blocklist. Patterns are
        # case-insensitive and must be quoted in YAML when they start with
        # * or contain {}/!/: sequences. Example:
        #   deny:
        #     - "git push --force*"
        #     - "*curl*|*sh*"
        "deny": [],
        # When true, /reload-mcp asks the user to confirm before rebuilding
        # the MCP tool set for the active session.  Reloading invalidates
        # the provider prompt cache (tool schemas are baked into the system
        # prompt), so the next message re-sends full input tokens — this can
        # be expensive on long-context or high-reasoning models.  Users click
        # "Always Approve" to silence the prompt permanently; that flips
        # this key to false.
        "mcp_reload_confirm": True,
        # When true, destructive session slash commands (/clear, /new, /reset,
        # /undo) ask the user to confirm before discarding conversation state.
        # Three-option prompt (Approve Once / Always Approve / Cancel) routed
        # through tools.slash_confirm — native yes/no buttons on Telegram,
        # Discord, and Slack; text fallback elsewhere.  Users click "Always
        # Approve" to silence the prompt permanently; that flips this key to
        # false.  TUI has its own modal overlay (HERMES_TUI_NO_CONFIRM=1 to
        # opt out there).
        "destructive_slash_confirm": True,
    },

    # Permanently allowed dangerous command patterns (added via "always" approval)
    "command_allowlist": [],
    # User-defined quick commands that bypass the agent loop (type: exec only)
    "quick_commands": {},

    # Per-platform system-prompt hint overrides. Lets an admin append to or
    # replace Hermes' built-in platform hint for a single messaging platform
    # (WhatsApp, Slack, Telegram, ...) without affecting other platforms.
    # Useful for enterprise/managed profiles that ship platform-aware skills.
    # Each key is a platform name; the value is either:
    #   { "append": "extra text" }   — keep the default hint, append text
    #   { "replace": "full text" }   — substitute the default hint entirely
    #   "extra text"                 — shorthand for { "append": ... }
    # `replace` wins over `append` if both are given. Example:
    #   platform_hints:
    #     whatsapp:
    #       append: >
    #         When tabular output would be useful, invoke the
    #         table_formatting skill instead of emitting a Markdown table.
    "platform_hints": {},

    # Shell-script hooks — declarative bridge that invokes shell scripts
    # on plugin-hook events (pre_tool_call, post_tool_call, pre_llm_call,
    # subagent_stop, etc.).  Each entry maps an event name to a list of
    # {matcher, command, timeout} dicts.  First registration of a new
    # command prompts the user for consent; subsequent runs reuse the
    # stored approval from ~/.hermes/shell-hooks-allowlist.json.
    # See `website/docs/user-guide/features/hooks.md` for schema + examples.
    "hooks": {},

    # Auto-accept shell-hook registrations without a TTY prompt.  Also
    # toggleable per-invocation via --accept-hooks or HERMES_ACCEPT_HOOKS=1.
    # Gateway / cron / non-interactive runs need this (or one of the other
    # channels) to pick up newly-added hooks.
    "hooks_auto_accept": False,
    # Custom personalities — add your own entries here
    # Supports string format: {"name": "system prompt"}
    # Or dict format: {"name": {"description": "...", "system_prompt": "...", "tone": "...", "style": "..."}}
    "personalities": {},

    # Pre-exec security scanning via tirith
    "security": {
        "allow_private_urls": False,  # Allow requests to private/internal IPs (for OpenWrt, proxies, VPNs)
        "redact_secrets": True,
        "tirith_enabled": True,
        "tirith_path": "tirith",
        "tirith_timeout": 5,
        "tirith_fail_open": True,
        "website_blocklist": {
            "enabled": False,
            "domains": [],
            "shared_files": [],
        },
        # Acknowledged supply-chain security advisories. Each entry is the
        # ID of an advisory the user has read and acted on (uninstalled the
        # compromised package, rotated credentials). Acked advisories no
        # longer trigger the startup banner. Add via `hermes doctor --ack
        # <id>`; remove by editing the list directly. See
        # ``hermes_cli/security_advisories.py`` for the catalog.
        "acked_advisories": [],
        # Allow Hermes to lazy-install opt-in backend packages from PyPI
        # the first time the user enables a backend that needs them
        # (e.g. installing ``elevenlabs`` when the user picks ElevenLabs as
        # their TTS provider). Set to false to require explicit
        # ``pip install`` for everything beyond the base set — appropriate
        # for restricted networks, audited environments, or air-gapped
        # systems where any runtime install is unacceptable.
        "allow_lazy_installs": True,
        "gliguard": {
            "enabled": False,
            "mode": "shadow",
            "url": "http://127.0.0.1:8766/moderate",
            "timeout_ms": 500,
            "fail_open": True,
            "shadow_log_path": "",
        },
    },

    "cron": {
        # Fail closed when an unpinned job's current global model/provider
        # differs from its creation-time snapshot. This prevents unattended
        # jobs from silently inheriting a paid default. Set to false only when
        # jobs should deliberately track changing global inference defaults.
        "model_drift_guard": True,
        # Default inference model for cron jobs (Axis A — WHAT model an
        # agent job runs on). Resolution at fire time: per-job user pin >
        # cron.model > global model.default. When set, unpinned jobs follow
        # this deliberately, so the #44585 model-drift fail-closed guard does
        # not engage for the model axis — cron spend no longer shadows chat
        # `/model` switches. Empty string = fall through to model.default.
        "model": "",
        # Inference provider paired with cron.model (NOT the scheduler
        # provider below). Empty string = resolve from global config.
        "model_provider": "",
        # Active cron SCHEDULER provider (Axis B — the trigger that decides
        # WHEN a due job fires). Empty string = the built-in in-process 60s
        # ticker (default). Name an installed provider (plugins/cron_providers/<name>/ or
        # $HERMES_HOME/plugins/<name>/) to relocate the trigger — e.g. "chronos",
        # the NAS-mediated managed-cron provider for scale-to-zero deployments.
        # An unknown or unavailable provider falls back to the built-in, so cron
        # never loses its trigger.
        "provider": "",
        # Chronos (NAS-mediated managed cron) settings. Only consulted when
        # provider == "chronos". All non-secret (URLs + the JWT audience): the
        # agent holds NO external-scheduler credentials. For hosted agents, NAS
        # sets these at provision time. The outbound provision call reuses the
        # agent's existing Nous Portal token — there is no token key here.
        "chronos": {
            # NAS / portal base URL the agent calls to arm/cancel one-shots
            # and that mints the inbound fire JWT (used as the expected issuer).
            "portal_url": "https://portal.nousresearch.com",
            # The agent's OWN publicly-reachable base URL for NAS→agent fires
            # (NAS POSTs {callback_url}/api/cron/fire). Empty → Chronos is
            # unavailable and the resolver falls back to the built-in ticker.
            "callback_url": "",
            # This agent's expected JWT audience (e.g. "agent:{instance_id}").
            "expected_audience": "",
            # NAS JWKS URL for verifying the inbound fire JWT's signature.
            # Empty → the fire endpoint refuses all tokens (no unsigned decode).
            "nas_jwks_url": "",
        },
        # Wrap delivered cron responses with a header (task name) and footer
        # ("The agent cannot see this message").  Set to false for clean output.
        "wrap_response": True,
        # Make cron deliveries CONTINUABLE: a user can reply to a cron brief
        # and the agent has it in context (no "what is Task #2?" amnesia).
        # Default False preserves the historical isolation guarantee (cron
        # deliveries live only in the cron job's own session). Per-job
        # `attach_to_session` overrides this for a single job.
        #
        # Behaviour is THREAD-PREFERRED, scoped to the job's origin chat:
        #   - Thread-capable platforms (Telegram forum/DM topics, Discord
        #     threads, Slack threads): a dedicated thread is opened for the job
        #     via the adapter's create_handoff_thread, the brief is delivered
        #     into it, and that thread's session is seeded so the user's reply
        #     in-thread continues with full context. Each continuable job gets
        #     its own scrollback, isolated from the parent channel.
        #   - DM-only platforms (WhatsApp / Signal / SMS): no threads exist, so
        #     the brief is mirrored into the origin DM session instead — the
        #     DM itself is the continuation surface.
        # Both paths ride the shipped gateway.mirror.mirror_to_session and are
        # alternation- and cache-safe (appended at a turn boundary, never
        # mid-loop, never mutating the cached system prompt). Only the origin
        # chat is ever touched — fan-out / broadcast targets are never mirrored.
        "mirror_delivery": False,
        # Maximum number of due jobs to run in parallel per tick.
        # null/0 = unbounded (limited only by thread count).
        # 1 = serial (pre-v0.9 behaviour).
        # Also overridable via HERMES_CRON_MAX_PARALLEL env var.
        "max_parallel_jobs": None,
        # Per-job output-file retention: save_job_output keeps the N most
        # recent .md files and prunes older ones. 0 or negative disables
        # pruning (for operators who manage cleanup externally). Default 50.
        "output_retention": 50,
        # Timeout (seconds) for SessionDB() init inside cron jobs.
        # SessionDB opens/migrates state.db synchronously and has no timeout
        # of its own against a wedged sqlite3.connect. An unbounded hang here
        # wedges the job's dispatch guard forever. Also overridable via
        # HERMES_CRON_SESSION_DB_TIMEOUT env var. 0 = unlimited (skip the bound).
        "session_db_timeout_seconds": 10,
    },

    # Kanban multi-agent coordination — controls the dispatcher loop that
    # spawns workers for ready tasks. The dispatcher ticks every N seconds
    # (default 60), reclaims stale claims, promotes dependency-satisfied
    # todos to ready, and fires `hermes -p <assignee> chat -q ...` for
    # each claimable ready task. One dispatcher per profile is sufficient;
    # running more than one on the same kanban.db will race for claims.
    "kanban": {
        # Auto-subscribe the originating gateway/TUI session to task
        # completion + block events when ``kanban_create`` is called from
        # inside a session that has a persistent delivery channel. The
        # agent that dispatched the task will get notified automatically
        # instead of having to poll. Disable to mirror pre-feature
        # behaviour — e.g. for a profile that prefers explicit
        # ``kanban_notify-subscribe`` calls per task.
        "auto_subscribe_on_create": True,
        # Run the dispatcher inside the gateway process. On by default —
        # the cost is ~300µs every `dispatch_interval_seconds` when idle,
        # and gateway is the supervisor users already have. Set to false
        # only if you run the dispatcher as a separate systemd unit or
        # don't want the gateway to spawn workers.
        "dispatch_in_gateway": True,
        # Seconds between dispatcher ticks (idle or not). Lower = snappier
        # pickup of newly-ready tasks; higher = less SQL pressure.
        "dispatch_interval_seconds": 60,
        # Auto-block after this many consecutive non-success attempts for the
        # same task/profile (spawn_failed, timed_out, or crashed). Reassignment
        # resets the streak for the new profile.
        "failure_limit": 2,
        # Worker stdout/stderr logs rotate at spawn time. Defaults preserve
        # the historical 2 MiB + one-backup behavior; long-running workers can
        # raise these to keep more early failure evidence.
        "worker_log_rotate_bytes": 2 * 1024 * 1024,
        "worker_log_backup_count": 1,
        # Profile assigned to the root/orchestration task after Triage
        # decomposition. When unset, falls back to the default profile (the
        # one `hermes` launches with no -p flag). This does not control the
        # decomposer prompt, model, or skills; configure that LLM path under
        # auxiliary.kanban_decomposer.
        "orchestrator_profile": "",
        # Where a child task lands if the orchestrator can't match an
        # assignee to any installed profile. When unset, falls back to the
        # default profile. A task never ends up with assignee=None.
        "default_assignee": "",
        # Per-profile concurrency cap (#21582). When set to a positive int,
        # no single profile can have more than N workers running at once,
        # even if the global max_in_progress / max_spawn caps would allow
        # it. Tasks blocked this way defer to the next dispatcher tick.
        # Unset (None) means "no per-profile cap" — backward-compatible
        # with existing installs. Useful for fan-out workflows that would
        # otherwise saturate one profile's local model / API quota /
        # browser pool while leaving other profiles idle.
        "max_in_progress_per_profile": None,
        # When true, the kanban dispatcher auto-runs the decomposer on
        # tasks that land in Triage (every dispatcher tick). When false,
        # decomposition is manual via `hermes kanban decompose <id>` or
        # the dashboard's Decompose button.
        "auto_decompose": True,
        # Max triage tasks to decompose per dispatcher tick. Prevents a
        # large bulk-load of triage tasks from spending a burst of aux
        # LLM calls in one tick. Excess tasks defer to the next tick.
        "auto_decompose_per_tick": 3,
        # Stale detection: running tasks that have exceeded this many
        # seconds without a heartbeat (since ``last_heartbeat_at``) are
        # auto-reclaimed to ``ready`` on the next dispatcher tick. The
        # worker process (if still running host-locally) is terminated
        # before the reclaim.  0 disables stale detection entirely.
        "dispatch_stale_timeout_seconds": 14400,
    },

    # execute_code settings — controls the tool used for programmatic tool calls.
    "code_execution": {
        # Execution mode:
        #   project (default) — scripts run in the session's working directory
        #     with the active virtualenv/conda env's python, so project deps
        #     (pandas, torch, project packages) and relative paths resolve.
        #   strict            — scripts run in an isolated temp directory with
        #     hermes-agent's own python (sys.executable). Maximum isolation
        #     and reproducibility; project deps and relative paths won't work.
        # Env scrubbing (strips *_API_KEY, *_TOKEN, *_SECRET, ...) and the
        # tool whitelist apply identically in both modes.
        "mode": "project",
    },

    # Tool Search (progressive disclosure for large tool surfaces).
    # When the model is connected to many MCP servers or non-core plugin
    # tools, their JSON schemas can consume a substantial fraction of the
    # context window on every turn. When enabled, those tools are replaced
    # in the model-facing tools array with three bridge tools —
    # tool_search / tool_describe / tool_call — and surfaced on demand.
    #
    # Core Hermes tools (terminal, read_file, write_file, patch,
    # search_files, todo, memory, browser_*, etc.) are NEVER deferred.
    # See tools/tool_search.py for full design notes and the
    # openclaw-tool-search-report PDF in this PR for the rationale.
    "tools": {
        "tool_search": {
            # Tiered disclosure: any deferrable (MCP/plugin) tool activates
            # the bridge; the listing then scales with catalog size.
            #   Tier 0 — no MCP/plugin tools: everything stays eager.
            #   Tier 1 — catalog listing fits the budget: bridge + skills-style
            #     name+description manifest (degrades to names-only).
            #   Tier 2 — per-tool listing over budget even names-only (e.g.
            #     Cloudflare's ~3,300-tool flat API surface): bare bridge +
            #     a one-line-per-server summary (name + tool count) so the
            #     model knows which domains are reachable; individual tools
            #     discoverable through tool_search only.
            # "auto"/"on" — activate when at least one deferrable tool exists.
            # "off" — disable entirely. Tools-array assembly is a pass-through.
            "enabled": "auto",
            # Listing budget as a percentage of the active model's context
            # length. Effective budget = min(this % of context,
            # listing_max_tokens). Range 0..100.
            "threshold_pct": 5,
            # When the model calls tool_search without a ``limit`` argument,
            # how many hits to return. Range 1..max_search_limit.
            "search_default_limit": 5,
            # Hard upper bound the model can request via ``limit``. Range 1..50.
            "max_search_limit": 20,
            # Skills-style catalog listing embedded in the tool_search bridge
            # description: every deferred tool's name + first sentence of its
            # description (≤60 chars), grouped by MCP server / toolset. Keeps
            # capabilities discoverable while schemas stay deferred.
            # "auto" (default) — include when the listing fits the budget
            #   (falls back to names-only, then to the bare tier-2 bridge).
            # "on"  — same rendering, but explicit intent to always list.
            # "off" — always the bare bridge (tier 2 for every catalog).
            "listing": "auto",
            # Absolute cap on the embedded listing in tokens (chars/4
            # estimate), regardless of context size. Range 200..60000.
            "listing_max_tokens": 20000,
        },
    },

    # Logging — controls file logging to ~/.hermes/logs/.
    # agent.log captures INFO+ (all agent activity); errors.log captures WARNING+.
    "logging": {
        "level": "INFO",       # Minimum level for agent.log: DEBUG, INFO, WARNING
        "max_size_mb": 5,      # Max size per log file before rotation
        "backup_count": 3,     # Number of rotated backup files to keep
    },

    # Remotely-hosted model catalog manifest.  When enabled, the CLI fetches
    # curated model lists for OpenRouter and Nous Portal from this URL,
    # falling back to the in-repo snapshot on network failure.  Lets us
    # update model picker lists without shipping a hermes-agent release.
    # The default URL is served by the docs site GitHub Pages deploy.
    "model_catalog": {
        "enabled": True,
        "url": "https://hermes-agent.nousresearch.com/docs/api/model-catalog.json",
        # Disk cache TTL in hours.  Beyond this, the CLI refetches on the
        # next /model or `hermes model` invocation; network failures
        # silently fall back to the stale cache.
        "ttl_hours": 1,
        # Optional per-provider override URLs for third parties that want
        # to self-host their own curation list using the same schema.
        # Example:
        #   providers:
        #     openrouter:
        #       url: https://example.com/my-curation.json
        "providers": {},
    },

    # Network settings — workarounds for connectivity issues.
    "network": {
        # Force IPv4 connections.  On servers with broken or unreachable IPv6,
        # Python tries AAAA records first and hangs for the full TCP timeout
        # before falling back to IPv4.  Set to true to skip IPv6 entirely.
        "force_ipv4": False,
    },

    # Gateway settings — control how messaging platforms (Telegram, Discord,
    # Slack, etc.) deliver agent-produced files as native attachments.
    "gateway": {
        # Durable delivery-obligation ledger: final agent responses are
        # recorded in state.db around the platform send, and a gateway that
        # died between finalize and platform ACK redelivers the stored
        # response on the next boot (ambiguous cases carry a visible
        # "recovered reply — may be a duplicate" marker; honest
        # at-least-once). Disable to lose in-flight final responses on
        # crash/restart, as before.
        "delivery_ledger": True,

        # Seconds the gateway waits for a single messaging platform to finish
        # connecting during startup (and on reconnect). Discord in particular
        # can blow past the old fixed 30s when an account has many slash
        # commands to sync (#19776: 90-173 skills → ~28-31s sync). Raise this
        # if your gateway hits "discord connect timed out" / "Timeout waiting
        # for connection to Discord" restart loops. ``0`` or negative disables
        # the timeout entirely (wait indefinitely). Bridged at startup to the
        # internal HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT env var, which still
        # works as a manual override and wins if set explicitly.
        "platform_connect_timeout": 30,

        # In-process event-loop liveness watchdog (#69089). A daemon OS thread
        # probes the gateway asyncio loop; after consecutive missed probes it
        # dumps all-thread stacks and hard-exits with the service-restart exit
        # code so the supervisor (systemd/launchd) revives the process instead
        # of leaving a wedged-but-alive zombie. Set to false to disable.
        "loop_watchdog": True,

        # Whether the gateway keeps writing the legacy sessions.json mirror of
        # its routing index. The primary copy lives in state.db (the
        # gateway_routing table). Default True for backward compatibility with
        # external tooling and downgrade safety; set to false to stop
        # producing ~/.hermes/sessions/sessions.json entirely.
        "write_sessions_json": True,

        # Scale-to-zero idle detection (Phase 0). The gateway watches for idle
        # and, when an instance is opted in via the NAS "Labs" toggle (carried as
        # the HERMES_SCALE_TO_ZERO env stamp) AND messaging is relay-only/absent
        # AND a wakeUrl is registered, drives the relay transport dormant so the
        # platform (e.g. Fly autostop:"suspend") can suspend the now-idle machine;
        # it wakes on the connector's wakeUrl poke. This is the idle TIMEOUT only
        # — whether the feature is enabled at all is the Labs toggle, never a
        # config key (decisions.md D2/D11). 0/negative falls back to the default.
        "scale_to_zero": {
            "idle_timeout_minutes": 5,
        },

        # Auto-resume restart-loop breaker (#30719, defense-3). When the
        # gateway is killed mid-turn (SIGTERM) and revived by a supervisor
        # (launchd KeepAlive / systemd Restart=), it auto-resumes the
        # restart-interrupted session on the next boot. If the resumed turn
        # keeps triggering another kill (e.g. the agent runs a raw
        # `launchctl kickstart ai.hermes.gateway` that defenses 1-2 don't
        # cover), the result is a tight SIGTERM-respawn loop. This breaker
        # counts restart-interrupted boots in a rolling window and, once
        # `max_restarts` boots happen within `window_seconds`, SKIPS
        # auto-resume for that boot — the gateway still starts and serves
        # real inbound messages, it just stops replaying the session that
        # keeps killing it. Set `max_restarts` to 0 to disable the breaker.
        "restart_loop_guard": {
            "max_restarts": 3,
            "window_seconds": 60,
        },

        # Portable respawn-storm circuit breaker (complements
        # ``restart_loop_guard`` above). Counts gateway (re)starts in a sliding
        # window and, when too many land, sleeps an exponential backoff before
        # booting so a crash-looping supervisor (launchd KeepAlive, systemd
        # Restart=always) can't hammer the process into a respawn storm.
        # ``max_starts <= 0`` disables the breaker. The env vars
        # ``HERMES_GATEWAY_MAX_STARTS`` / ``HERMES_GATEWAY_START_WINDOW_S``
        # override these defaults for escape-hatch use.
        "respawn_storm": {
            "max_starts": 5,
            "window_seconds": 120,
        },

        # Inject a human-readable timestamp prefix (e.g.
        # "[Tue 2026-04-28 13:40:53 CEST]") onto user messages IN THE MODEL'S
        # CONTEXT so the agent has temporal awareness of when each message was
        # sent. Off by default — when off, the model sees clean message text.
        # Persisted transcripts always stay clean (the timestamp is stored as
        # message metadata regardless of this toggle), so turning it on later
        # surfaces send-times for past messages too.
        "message_timestamps": {
            "enabled": False,
        },

        # Maximum bytes for an inbound image / audio / video payload the
        # gateway will buffer into memory and cache to disk. Inbound media is
        # read fully into RAM before being written, so an unbounded upload
        # (Discord Nitro allows 500 MB) or a remote media URL pointing at a
        # huge file can spike memory and OOM-kill the gateway on constrained
        # deployments. Enforced in the shared cache helpers
        # (gateway/platforms/base.py), so the cap holds across every platform
        # adapter. ``0`` disables the cap. Default 128 MiB.
        "max_inbound_media_bytes": 134217728,

        # When false (default), any file path the agent emits is delivered
        # as a native attachment as long as it isn't under the credential /
        # system-path denylist (/etc, /proc, ~/.ssh, ~/.aws, ~/.hermes/.env,
        # auth.json, etc.). This matches the symmetry of inbound delivery
        # — we accept any document type the user uploads, and the agent
        # can hand back any file that isn't a credential.
        #
        # When true, fall back to the older allowlist+recency-window
        # behavior: files must live under the Hermes cache, under
        # ``media_delivery_allow_dirs``, or be freshly produced inside the
        # ``trust_recent_files_seconds`` window. Recommended for
        # public-facing gateways where prompt injection from one user
        # shouldn't be able to exfiltrate the host's secrets to that same
        # user. Bridged to HERMES_MEDIA_DELIVERY_STRICT.
        "strict": False,
        # Extra directories from which model-emitted bare file paths may be
        # uploaded as native gateway attachments. Files inside the Hermes
        # cache (~/.hermes/cache/{documents,images,audio,video,screenshots})
        # are always trusted; this list adds operator-controlled roots
        # (project dirs, scratch dirs, mounted shares). Accepts a list of
        # absolute paths or a single os.pathsep-separated string. Bridged
        # to HERMES_MEDIA_ALLOW_DIRS at gateway startup. Tilde paths are
        # expanded. Honored in both default and strict mode.
        "media_delivery_allow_dirs": [],
        # When true, files whose mtime is within ``trust_recent_files_seconds``
        # of "now" are trusted for native delivery even outside the cache /
        # operator allowlist — useful for ``pandoc -o /tmp/report.pdf`` or
        # PDFs the agent writes into a working directory. System paths
        # (/etc, /proc, ~/.ssh, ~/.aws, etc.) remain blocked regardless.
        # Disable to fall back to pure-allowlist mode. Bridged to
        # HERMES_MEDIA_TRUST_RECENT_FILES. Only consulted when ``strict``
        # is true; in default mode the denylist alone gates delivery.
        "trust_recent_files": True,
        # Recency window in seconds. 600 (10 min) comfortably covers a
        # multi-tool agent turn. Bridged to HERMES_MEDIA_TRUST_RECENT_SECONDS.
        # Only consulted when ``strict`` is true.
        "trust_recent_files_seconds": 600,

        # OpenAI-compatible API server platform
        # (gateway/platforms/api_server.py).
        "api_server": {
            # Maximum number of agent runs the API server will service
            # concurrently. Requests to /v1/chat/completions, /v1/responses,
            # and /v1/runs that arrive while this many runs are already
            # in flight are rejected with HTTP 429 + a Retry-After header,
            # bounding CPU / memory / upstream-LLM-quota exhaustion from a
            # request flood. Set to 0 to disable the cap entirely.
            "max_concurrent_runs": 10,
        },
    },

    # Real-time token streaming to messaging platforms (Telegram, Discord,
    # Slack, etc.). Read at the top level by the gateway; absent this block the
    # gateway falls back to these same defaults, so adding it here only makes
    # the feature discoverable in config.yaml — it does not change behavior.
    #
    # Disabled by default: streaming costs extra edit/draft API calls per
    # response. Set ``enabled: true`` and restart the gateway to turn it on.
    "streaming": {
        # Master switch. When false, each response is delivered as a single
        # final message (no progressive updates).
        "enabled": False,
        # Transport selection:
        #   "auto"  — prefer native draft streaming where the platform
        #             supports it (Telegram DMs via sendMessageDraft,
        #             Bot API 9.5+) and fall back to edit-based elsewhere.
        #             Safe global default: platforms without draft support
        #             (Discord, Slack, Matrix, Telegram groups) transparently
        #             use the edit path, so "auto" only upgrades chats that
        #             can render the smoother native preview.
        #   "draft" — explicitly request native drafts; falls back to edit
        #             when the platform/chat doesn't support them.
        #   "edit"  — progressive editMessageText only (legacy behavior).
        #   "off"   — disable streaming entirely (same as enabled: false).
        "transport": "auto",
        # Minimum seconds between progressive edits — tuned for Telegram's
        # ~1 edit/s flood envelope.
        "edit_interval": 0.8,
        # Flush the buffer to the platform once this many characters have
        # accumulated, so short replies feel near-instant.
        "buffer_threshold": 24,
        # Cursor glyph appended to the in-progress message while streaming.
        "cursor": " \u2589",
        # When >0, the final edit for a long-running streamed response is
        # delivered as a fresh message if the preview has been visible at
        # least this many seconds, so the platform timestamp reflects
        # completion time. Telegram only; other platforms ignore it.
        "fresh_final_after_seconds": 0.0,
    },

    # Session storage — controls automatic cleanup of ~/.hermes/state.db.
    # state.db accumulates every session, message, tool call, and FTS5 index
    # entry forever.  Without auto-pruning, a heavy user (gateway + cron)
    # reports 384MB+ databases with 68K+ messages, which slows down FTS5
    # inserts, /resume listing, and insights queries.
    "sessions": {
        # When true, prune ended sessions inactive for retention_days once
        # per (roughly) min_interval_hours at CLI/gateway/cron startup.
        # Activity is the latest message timestamp, falling back to creation
        # time for empty sessions. Active sessions are always preserved.
        # Default false: session history is valuable for search recall, and
        # silently deleting it could surprise users.  Opt in explicitly.
        "auto_prune": False,
        # How many inactive days of ended-session history to keep. Matches
        # the default of ``hermes sessions prune``.
        "retention_days": 90,
        # When true, auto-archive (soft-hide, never delete) sessions that
        # haven't been touched in ``auto_archive_days`` days, once per
        # (roughly) min_interval_hours.  "Touched" is last activity, not
        # creation, so an old-but-recently-used session is spared.  Pinned
        # sessions are always exempt.  Off by default — opt in explicitly.
        "auto_archive": False,
        # Idle threshold (days of no activity) before auto-archive hides a
        # session.  Only applies when auto_archive is true.
        "auto_archive_days": 3,
        # VACUUM after a prune that actually deleted rows.  SQLite does not
        # reclaim disk space on DELETE — freed pages are just reused on
        # subsequent INSERTs — so without VACUUM the file stays bloated
        # even after pruning.  VACUUM blocks writes for a few seconds per
        # 100MB, so it only runs at startup, and only when prune deleted
        # ≥1 session.
        "vacuum_after_prune": True,
        # Minimum hours between auto-maintenance runs (avoids repeating
        # the sweep on every CLI invocation).  Tracked via state_meta in
        # state.db itself, so it's shared across all processes.
        "min_interval_hours": 24,
        # Legacy per-session JSON snapshot writer.  When true, the agent
        # rewrites ``~/.hermes/sessions/session_{sid}.json`` on every turn
        # boundary with the full message list.  state.db is canonical and
        # has every field the snapshot stored (plus per-message timestamps
        # and token counts), so this is off by default — the snapshots had
        # no consumer outside their own overwrite guard and accumulated
        # GBs of disk on heavy users.  Opt in only if you have an external
        # tool that consumes the JSON files directly.
        "write_json_snapshots": False,
        # Search-index (FTS) storage optimization — the compact v23 layout
        # that drops duplicate content copies and stops trigram-indexing tool
        # output (typically reclaims ~60%+ of state.db on heavy users). It is
        # OPT-IN: existing databases keep their working legacy index until the
        # user runs `hermes sessions optimize-storage`, because the rebuild is
        # disk-heavy and long on large DBs (see that command's disk preflight).
        #
        #   "advise" (default): `hermes update` prints a one-line notice with
        #     the reclaimable size and the command, when a legacy index is
        #     detected. Nothing is changed automatically.
        #   "require": the notice is shown as a REQUIRED upgrade (firmer copy),
        #     and future tooling may gate on it. Flip this default in a future
        #     release when we're ready to make the v23 layout mandatory — the
        #     command, progress bar, and resumability are already in place, so
        #     enforcement is a copy/gating change, not new migration code.
        #   "off": suppress the notice entirely.
        "fts_optimize_notice": "advise",
        # CJK-bigram search index (messages_fts_cjk, cjk_unicode61 loadable
        # tokenizer). When the extension is built (native/fts5_cjk/build.sh →
        # ~/.hermes/lib/libfts5_cjk.so), 1-2 char CJK terms (일본, 项目, ...)
        # get index-speed exact matching instead of LIKE full-table scans.
        # True (default): use the index when the extension is present; the
        # setting is inert when it isn't. False: never load the extension or
        # serve the cjk index. Bridged to HERMES_CJK_FTS (internal carrier).
        "cjk_fts": True,
        # Slow session-search log threshold in milliseconds: searches at or
        # above it log one INFO line with the routing path taken (fts_cjk /
        # fts5 / trigram / like_scan) so latency regressions stay
        # attributable per query shape. 0 logs every search. Bridged to
        # HERMES_SEARCH_SLOW_MS (internal carrier).
        "search_slow_ms": 1000,
    },

    # Contextual first-touch onboarding hints (see agent/onboarding.py).
    # Each hint is shown once per install and then latched here so it
    # never fires again.  Users can wipe the section to re-see all hints.
    "onboarding": {
        "seen": {},
        # Structured profile-build path offered on the very first gateway
        # message ever. "ask" (default) -> offer to build a user profile
        # (opt-in, consent-gated; the agent asks before any lookup and never
        # reads connected accounts silently). "off" -> plain intro only.
        # The offer fires at most once (latched under onboarding.seen).
        "profile_build": "ask",
    },

    # Privacy-safe aggregate metrics written only to this profile's local
    # telemetry directory. Collection is opt-in and no remote sink exists.
    "telemetry": {
        "shared_metrics": {
            "enabled": False,
        },
    },

    # ``hermes update`` behaviour.
    "updates": {
        # Pre-update safety backup — ONE consolidated mechanism, three modes:
        #
        #   quick (default) — snapshot critical small state files (pairing
        #     JSONs, cron jobs, config.yaml, .env, auth.json, per-profile
        #     DBs) into <HERMES_HOME>/state-snapshots/ before the update.
        #     Files over 1 GiB (e.g. a bloated state.db) are skipped with a
        #     warning so the snapshot stays fast. Restore via ``/snapshot``.
        #     This is the #15733 (lost pairing data) / #34600 (emptied cron
        #     jobs) safety net.
        #   full — the quick snapshot PLUS a full ``hermes backup``-style zip
        #     of HERMES_HOME into <HERMES_HOME>/backups/, restorable with
        #     ``hermes import``. Can add minutes on large homes. This is the
        #     #48200 (wrong-path wipe) safety net. ``--backup`` forces this
        #     for a single run.
        #   off — no pre-update backup of any kind. ``--no-backup`` forces
        #     this for a single run.
        #
        # Legacy boolean values are honored: true -> full, false -> off.
        "pre_update_backup": "quick",
        # How many full pre-update backup zips to retain (mode ``full``).
        # Older ones are pruned automatically after each successful backup.
        # Values below 1 are floored to 1 — the backup just created is
        # always preserved. The quick snapshot always keeps exactly 1.
        "backup_keep": 5,
        # What `hermes update` does with uncommitted local changes to the
        # source tree when it runs NON-interactively — i.e. triggered from
        # the desktop/chat app or the gateway, where there's no TTY to answer
        # a restore prompt. Interactive (terminal) updates are unaffected:
        # they always stash the changes and ask whether to restore, exactly
        # as they always have.
        #   "stash"   — auto-stash the changes, pull, then auto-restore them
        #               on top of the updated code (the safe default; nothing
        #               is ever lost — conflicts are preserved in a git stash).
        #   "discard" — auto-stash the changes and throw the stash away after
        #               the pull. Use this only if you never intend to keep
        #               local edits to the source tree on this machine.
        #               Stash-and-drop (not `reset --hard` + `clean -fd`) so
        #               ignored paths — node_modules, venv, build outputs —
        #               are never touched.
        "non_interactive_local_changes": "stash",
        # Refresh an already-installed cua-driver during `hermes update`.
        # The refresh is best-effort and macOS-only. Turn this off if the
        # upstream installer is not appropriate for the machine, for example
        # on non-admin accounts where `/Applications` is not writable.
        "refresh_cua_driver": True,
    },

    # Language Server Protocol — semantic diagnostics from real
    # language servers (pyright, gopls, rust-analyzer, etc.) wired
    # into the post-write lint check used by ``write_file`` and
    # ``patch``.
    #
    # LSP is gated on git-workspace detection: when the agent's
    # cwd (or the file being edited) is inside a git worktree, LSP
    # runs against that workspace.  When neither is in a git repo,
    # LSP stays dormant and the in-process syntax check is the only
    # tier — handy for Telegram/Discord chats where the cwd is the
    # user's home directory.
    "lsp": {
        # Master toggle.  Setting this to false disables the entire
        # subsystem — no servers spawn, no background event loop, no
        # cost.
        "enabled": True,

        # Diagnostic-wait mode for the post-write check.
        # ``"document"`` waits up to ``wait_timeout`` seconds for the
        # current file's diagnostics; ``"full"`` additionally requests
        # workspace-wide diagnostics (slower).
        "wait_mode": "document",
        "wait_timeout": 5.0,

        # How to handle missing server binaries.
        # ``"auto"`` — try to install via npm/go/pip into
        #              ``<HERMES_HOME>/lsp/bin/`` on first use.
        # ``"manual"`` — only use binaries already on PATH.
        # ``"off"`` — alias for ``manual``.
        "install_strategy": "auto",

        # Per-server overrides.  Each key is a server_id from the
        # registry (``pyright``, ``typescript``, ``gopls``,
        # ``rust-analyzer``, etc.) and accepts:
        #   disabled: true
        #     — skip this server even when its extensions match
        #   command: ["full/path/to/server", "--stdio"]
        #     — pin a custom binary path; bypasses auto-install
        #   env: {"KEY": "value"}
        #     — extra env vars passed to the spawned process
        #   initialization_options: {...}
        #     — merged into the LSP ``initializationOptions``
        # Empty by default; the registry defaults work for typical
        # setups.
        "servers": {},
    },


    # X (Twitter) Search via xAI's built-in x_search Responses tool.
    # The tool registers when xAI credentials are available (SuperGrok
    # OAuth or XAI_API_KEY) AND the x_search toolset is enabled in
    # `hermes tools`. These settings tune the backing Responses API call.
    "x_search": {
        # xAI model used for the Responses call. grok-4.5 is the
        # recommended default; any Grok model with x_search tool
        # access works.
        "model": "grok-4.5",
        # Optional reasoning effort sent to xAI Responses API models that
        # support it. Leave null to preserve the selected model's default.
        "reasoning_effort": None,
        # Request timeout in seconds (minimum 30). x_search can take
        # 60-120s for complex queries — the default is generous.
        "timeout_seconds": 180,
        # Number of automatic retries on 5xx / ReadTimeout / ConnectionError.
        # Each retry backs off (1.5x attempt seconds, capped at 5s).
        "retries": 2,
    },

    # =========================================================================
    # External secret sources
    # =========================================================================
    # Pull credentials from external secret managers at process startup
    # rather than storing them in ~/.hermes/.env.
    "secrets": {
        # Optional explicit ordering of enabled secret sources.  When
        # omitted, sources run in registration order (bundled first,
        # then plugin-registered).  Regardless of this list, "mapped"
        # sources (explicit VAR→ref bindings, e.g. a future 1Password
        # env: map) always take precedence over "bulk" sources
        # (project dumps like Bitwarden BSM), and the first source to
        # claim a var wins — later claims are skipped with a warning.
        # Example: sources: [onepassword, bitwarden]
        # "sources": [],
        "bitwarden": {
            # Master switch.  When false, BSM is never contacted and the
            # bws binary is never auto-installed — same as not having
            # this section at all.
            "enabled": False,
            # Name of the env var that holds the Bitwarden machine-account
            # access token.  This is the one bootstrap secret; it lives
            # in ~/.hermes/.env (or your shell) and never in config.yaml.
            "access_token_env": "BWS_ACCESS_TOKEN",
            # UUID of the BSM project to sync from.
            "project_id": "",
            # Seconds to reuse a fresh disk/memory cache entry before contacting
            # Bitwarden again. 0 disables normal fresh-cache reuse.
            "cache_ttl_seconds": 300,
            # Optional encrypted last-good fallback for network/timeout outages.
            # When enabled, successful BWS fetches write AES-GCM encrypted cache
            # material under ~/.hermes/cache/. If a later startup cannot reach
            # Bitwarden due to NETWORK/TIMEOUT, Hermes may use this encrypted
            # cache for up to max_stale_seconds. Auth failures do not fall back.
            "encrypted_cache": {
                "enabled": False,
                "max_stale_seconds": 0,
            },
            # When True, BSM values overwrite existing env vars.  Default
            # True because the point of using BSM is centralized rotation —
            # if .env had the final say, rotating in Bitwarden wouldn't
            # take effect until you also cleared the matching .env line.
            "override_existing": True,
            # When True, the bws binary is auto-downloaded into
            # ~/.hermes/bin/ on first use.  When False you must install
            # bws yourself and have it on PATH.
            "auto_install": True,
            # Bitwarden region / self-hosted endpoint.  Empty string
            # means use the bws CLI default (US Cloud,
            # https://vault.bitwarden.com).  Set to
            # https://vault.bitwarden.eu for EU Cloud, or your own URL
            # for self-hosted Bitwarden.  Plumbed into the bws subprocess
            # as BWS_SERVER_URL.  Prompted for during
            # `hermes secrets bitwarden setup`.
            "server_url": "",
        },
        "onepassword": {
            # Master switch.  When false, the op CLI is never invoked —
            # same as not having this section at all.
            "enabled": False,
            # Mapping of env-var name → 1Password secret reference
            # (op://vault/item/field).  Each entry is resolved with a
            # single `op read` at startup.
            "env": {},
            # Optional account shorthand / sign-in address passed as
            # `op read --account <account>`.  Empty = op's default account.
            "account": "",
            # Name of the env var holding a 1Password service-account token
            # for headless auth.  Sourced from ~/.hermes/.env (or the shell)
            # and exported to the op child as OP_SERVICE_ACCOUNT_TOKEN.
            # Leave the var unset to use an interactive/desktop op session.
            "service_account_token_env": "OP_SERVICE_ACCOUNT_TOKEN",
            # Optional absolute path to the op binary.  When set it is used
            # verbatim (PATH is not consulted) — pin this to avoid trusting
            # whatever `op` appears first on PATH.  Empty = resolve via PATH.
            "binary_path": "",
            # Seconds to cache resolved values in-process and on disk.  0
            # disables BOTH cache layers (no values are written to disk).
            "cache_ttl_seconds": 300,
            # When True (default), resolved values overwrite existing env
            # vars so rotating a secret in 1Password takes effect on next
            # start.  Flip to false to let .env / shell exports win locally.
            "override_existing": True,
        },
    },

    # Paste collapse thresholds (TUI + CLI).
    #
    # paste_collapse_threshold (default 5)
    #   Bracketed-paste handler. Pastes with this many newlines or more
    #   collapse to a file reference. Set 0 to disable.
    #
    # paste_collapse_threshold_fallback (default 5)
    #   Fallback heuristic for terminals without bracketed paste support.
    #   Same line count test but heuristically gated by chars-added /
    #   newlines-added to avoid false positives from normal typing.
    #   Set 0 to disable.
    #
    # paste_collapse_char_threshold (default 2000)
    #   Long single-line paste guard. Pastes whose total char length
    #   reaches this value collapse to a file reference even if line
    #   count is below the line threshold. Catches the "8000 chars of
    #   minified JSON / log output on one line" case. Set 0 to disable.
    "paste_collapse_threshold": 5,
    "paste_collapse_threshold_fallback": 5,
    "paste_collapse_char_threshold": 2000,

    # Computer Use (cua-driver) toolset settings.
    "computer_use": {
        # cua-driver ships with anonymous usage telemetry (PostHog) ENABLED
        # by default upstream. Hermes disables it for our users unless they
        # explicitly opt in here. When false (default), Hermes sets
        # CUA_DRIVER_RS_TELEMETRY_ENABLED=0 in the cua-driver child env for
        # every invocation (MCP backend, status, doctor, install). Set true
        # to let cua-driver use its own default (telemetry on).
        "cua_telemetry": False,
        # Cap driver screenshot longest edge (pixels) via set_config on
        # session start. Shrinks SOM multimodal payloads; 0 disables.
        "max_image_dimension": 1456,
        # Mode for capture_after follow-ups: som (screenshot + overlays —
        # default), ax (elements only, no PNG — faster), vision (pixels only).
        "capture_after_mode": "som",
        # Disable the cursor overlay rendered by cua-driver. The overlay
        # shows where agent actions land but can peg a core when idle
        # (macOS vImage redraw loop #47032; Linux/WSL2 idle spin #28152).
        # cua-driver ≥ 0.6.x supports --no-overlay; Hermes also calls
        # set_agent_cursor_enabled(false) after start_session when this is on.
        #   None  = auto-detect (off on macOS + headless/WSL2 Linux; on elsewhere)
        #   True  = always disable the overlay
        #   False = always enable the overlay
        "no_overlay": None,
    },

    # =========================================================================
    # Egress credential-injection proxy (iron-proxy)
    # =========================================================================
    # When enabled, outbound traffic from remote terminal sandboxes (Docker
    # today; Modal/SSH in follow-ups) is routed through a managed iron-proxy
    # subprocess.  The sandbox sees opaque proxy tokens; iron-proxy swaps in
    # real API credentials at the egress boundary.  Compromising the sandbox
    # leaks tokens that only work behind the configured trusted proxy boundary
    # (CA private key + proxy endpoint integrity are part of that boundary).
    #
    # Configure with `hermes egress setup`.  Disabled by default — the rest of
    # Hermes works exactly as before with `enabled: false`.
    "proxy": {
        # Master switch.  When false, iron-proxy is never started, no docker
        # mounts are added, no binaries are auto-installed — feature is a
        # complete no-op.
        "enabled": False,
        # Tunnel listener port.  Sandboxes get `HTTPS_PROXY=http://<host>:<port>`.
        # 9090 is the default; collide-aware setup wizard can reassign.
        "tunnel_port": 9090,
        # Auto-download the pinned iron-proxy binary into ~/.hermes/bin/ on
        # first use.  When false, you must place `iron-proxy` on PATH yourself.
        "auto_install": True,
        # Where iron-proxy looks up the real upstream secrets at egress time.
        # "env"        — process env (default; what bitwarden integration
        #                already populates if you use it)
        # "bitwarden"  — refetch via `bws secret list` on each proxy restart;
        #                rotation in the Bitwarden web app propagates without
        #                touching .env (requires `secrets.bitwarden.enabled`).
        "credential_source": "env",
        # When true, the Docker backend refuses to start a sandbox if the
        # proxy is enabled but not running.  False = fall back to direct
        # outbound with real credentials in the sandbox (the legacy posture).
        "enforce_on_docker": True,
        # NOTE: ``fail_on_uncovered_providers`` was removed.  It gated a
        # refuse-start when Anthropic / Azure OpenAI / Gemini env vars were
        # present — those providers are now first-class swapped providers
        # via per-provider match_headers rules (x-api-key, api-key,
        # x-goog-api-key), so the fail-closed tier is empty.  A leftover
        # key in existing user configs is ignored harmlessly.
        # When credential_source is bitwarden but the BWS access token /
        # project_id is missing OR the bws fetch returns no values for
        # mapped providers, the daemon raises by default.  Set this to
        # True to opt back in to the legacy "silently fall back to host
        # env" behaviour — useful for migrations where the operator wants
        # to switch credential_source to bitwarden but hasn't fully wired
        # BWS yet.  Defaults to false (strict).
        "allow_env_fallback": False,
        # SSRF deny list applied to outbound traffic.  Omit / leave empty
        # to use the safe default: loopback, link-local (incl. cloud
        # metadata IPs at 169.254.169.254), and RFC1918.  Set to an
        # explicit ``[]`` to opt out entirely (only sensible in hermetic
        # tests that need to reach a loopback upstream).
        "upstream_deny_cidrs": None,
        # Extra allowed upstream hosts beyond the bundled defaults (which
        # cover OpenRouter, OpenAI, Anthropic, Google, xAI, Mistral, Groq,
        # Together, DeepSeek, Nous).  Wildcards (`*.foo.com`) are supported.
        "extra_allowed_hosts": [],
    },

    # Hermes Desktop (Electron app) launch options. These only affect
    # `hermes desktop`; they do not touch the CLI/gateway.
    "desktop": {
        # Git repository discovery for the Desktop Projects sidebar. Empty
        # roots preserve the historical bounded scan of the user's home.
        "repo_scan_enabled": True,
        "repo_scan_roots": [],
        "repo_scan_exclude_paths": [],
        # Extra Electron command-line flags appended to every desktop launch,
        # e.g. ["--ozone-platform=x11"] on headless/VM X11 hosts that need an
        # explicit ozone backend, or GPU workaround flags. A list of strings;
        # a single string is also accepted and shell-split.
        "electron_flags": [],
        # GPU hardware acceleration policy for the desktop app:
        #   "auto"  - let the app detect remote displays (SSH/VNC/RDP) and
        #             disable GPU only then (default; current behavior).
        #   true    - always disable GPU acceleration (software rendering).
        #             Use on no-GPU VMs / Proxmox hosts where the GPU path hangs.
        #   false   - always keep GPU acceleration on, even over a remote display.
        # Bridged to the HERMES_DESKTOP_DISABLE_GPU env var the Electron app reads.
        "disable_gpu": "auto",
        # macOS only: optional persistent code-signing identity (a cert in the
        # login keychain — a self-signed "Code Signing" cert from Keychain
        # Access works; no Apple Developer account needed) used to re-sign
        # locally rebuilt desktop apps. A certificate-anchored Designated
        # Requirement stays stable across rebuilds, so TCC grants (Full Disk
        # Access, Desktop/Downloads/Documents, Accessibility, Automation,
        # microphone) survive every update. Empty keeps the default stable
        # ad-hoc signing (identifier-pinned requirement).
        "macos_signing_identity": "",
        # Auto-continue a turn that was killed mid-run by an app/backend/machine
        # crash: resuming that session re-submits the interrupted prompt (shown
        # as a "resumed interrupted turn" event) if the interruption is fresh.
        # A stale interruption just shows the recovered partial transcript.
        "auto_continue": {
            "enabled": True,
            # How recent the interruption must be to auto-continue (minutes).
            "freshness_minutes": 15,
            # Crash-loop breaker: max automatic re-runs of one interrupted turn.
            "max_attempts": 2,
        },
    },


    # Google Vertex AI provider (Gemini via the OpenAI-compatible endpoint).
    # Auth is OAuth2 (short-lived access tokens minted from a service-account
    # JSON or Application Default Credentials) — NOT a static API key. The
    # credential *path* is a secret-adjacent pointer and lives in .env
    # (VERTEX_CREDENTIALS_PATH / GOOGLE_APPLICATION_CREDENTIALS); these two
    # settings are non-secret routing config and live here. Both are bridged to
    # the VERTEX_PROJECT_ID / VERTEX_REGION env vars the adapter reads, so an
    # explicit env var still wins over config.yaml.
    "vertex": {
        # GCP project ID. Empty → use the project_id embedded in the service
        # account JSON (or ADC-resolved project).
        "project_id": "",
        # Vertex region. "global" is required for the Gemini 3.x preview models
        # (regional endpoints silently 404 them). Override to a regional value
        # (e.g. "us-central1") only if your models are pinned to a region.
        "region": "global",
    },

    # Config schema version - bump this when adding new required fields
    "_config_version": 33,
}

# =============================================================================
# Config Migration System
# =============================================================================

# Track which env vars were introduced in each config version.
# Migration only mentions vars new since the user's previous version.
ENV_VARS_BY_VERSION: Dict[int, List[str]] = {
    3: ["FIRECRAWL_API_KEY", "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID", "FAL_KEY"],
    4: ["VOICE_TOOLS_OPENAI_KEY", "ELEVENLABS_API_KEY"],
    5: ["WHATSAPP_ENABLED", "WHATSAPP_MODE", "WHATSAPP_ALLOWED_USERS",
        "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS"],
    10: ["TAVILY_API_KEY"],
    11: ["TERMINAL_MODAL_MODE"]}

# Intentionally empty: the LLM provider is required but handled by the setup wizard's provider
# selection step, so no single env var is universally required.
REQUIRED_ENV_VARS = {}


def get_missing_env_vars(required_only: bool = False) -> List[Dict[str, Any]]:
    """Check which environment variables are missing."""
    groups = [(REQUIRED_ENV_VARS, True)]
    if not required_only:
        groups.append((OPTIONAL_ENV_VARS, False))
    return [
        {"name": var_name, **info, "is_required": is_required}
        for table, is_required in groups
        for var_name, info in table.items()
        if not get_env_value(var_name)]


def _split_key_path(key: str) -> list[str]:
    """Split a dotted config-key path, honoring backslash-escaped dots (``a\\.b`` -> ``a.b``).
    Backslashes before any other character are preserved verbatim.

    ``hermes config set`` uses ``.`` as the nesting separator, so a key that itself contains a literal dot
    (e.g. provider names like ``qwen3.5-397b-wafer``) was silently split into bogus nested segments
    (#84064).
    """
    parts: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(key):
        ch = key[i]
        if ch == "\\" and key[i + 1:i + 2] == ".":
            current.append(".")
            i += 2
            continue
        if ch == ".":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _greedy_literal_match(container: dict, parts: list) -> Optional[Tuple[str, int]]:
    """Return ``(literal_key, n_consumed)`` for the longest dotted literal key present in
    *container*, or None. With no multi-segment literal this is the historic plain-split walk.

    Dots in config key names are the norm, not the exception — model IDs (``grok-4.6``, ``glm-5.3``), Matrix
    room IDs (``!room:chat.example.cc``), and versioned provider names all embed dots. Users typing
    ``providers.myprov.models.grok-4.6.context_length`` do not know the escape syntax exists, so when
    navigating an EXISTING mapping we prefer an existing literal key equal to the dot-join of the next N
    path segments (longest match wins) over blindly splitting. See #84064 / #80006 / 91095 / #91607 /
    #99124.
    """
    if not isinstance(container, dict) or not parts:
        return None
    return next(
        ((".".join(parts[:n]), n) for n in range(len(parts), 0, -1) if ".".join(parts[:n]) in container),
        None)


def _phantom_sibling(container: dict, part: str) -> Optional[str]:
    """Existing literal dotted key that creating an intermediate mapping ``part`` would shadow
    (``grok-4`` beside ``grok-4.5``) — the write would produce a phantom sibling the runtime never
    reads, so callers fail loudly instead.

    Called when a write is about to CREATE a new intermediate mapping named ``part``. See #84064.
    """
    if not isinstance(container, dict):
        return None
    prefix = part + "."
    return next((k for k in container if isinstance(k, str) and k.startswith(prefix)), None)


def _set_nested(config, dotted_key: str, value):
    """Set a value at a dotted key path, creating intermediate dicts on demand.
    Numeric segments index lists; the index must already exist (lists are never grown).

    Guards against #17876: before this fix the code unconditionally replaced any non-dict value (including
    lists) with ``{}``, silently destroying list-typed config like ``custom_providers`` whenever a caller
    used an indexed path.
    Dotted key names (#84064 family): when navigating an existing mapping, an existing literal key equal to
    the dot-join of the next N segments is preferred over blind splitting (see ``_greedy_literal_match``),
    so ``models.grok-4.6.supports_vision`` lands on the real ``grok-4.6`` entry. And when a write WOULD
    create a new intermediate mapping that shadows an existing dotted sibling (``grok-4`` beside
    ``grok-4.5``), it raises ``ValueError`` instead of silently writing a phantom the runtime never reads.
    """
    parts = _split_key_path(dotted_key)
    current = config
    i = 0
    while i < len(parts):
        remaining = parts[i:]
        at_leaf = len(remaining) == 1
        if isinstance(current, list):
            part = remaining[0]
            if at_leaf:
                current[int(part)] = value
                return
            try:
                current = current[int(part)]
            except (TypeError, ValueError):
                raise TypeError(
                    f"Cannot navigate into list at key {dotted_key!r}: "
                    f"segment {part!r} is not a numeric index")
            i += 1
        elif isinstance(current, dict):
            match = _greedy_literal_match(current, remaining)
            if match is not None:
                key, consumed = match
                if i + consumed == len(parts):
                    current[key] = value
                    return
                # Preserve dicts and lists; replace scalar with a fresh dict.
                if not isinstance(current.get(key), (dict, list)):
                    current[key] = {}
                current = current[key]
                i += consumed
                continue
            part = remaining[0]
            if at_leaf:
                current[part] = value
                return
            shadowed = _phantom_sibling(current, part)
            if shadowed is not None:
                escaped = shadowed.replace(".", "\\.")
                raise ValueError(
                    f"Refusing to create nested key {part!r} in {dotted_key!r}: the mapping "
                    f"already contains a literal key {shadowed!r} that contains a dot. If you "
                    f"meant that key, escape its dots with a backslash (e.g. {escaped}).")
            current = current.setdefault(part, {})
            i += 1
        else:
            raise TypeError(f"Cannot navigate into {type(current).__name__} at key {dotted_key!r}")


def clear_model_endpoint_credentials(
    model_cfg: Dict[str, Any], *, clear_api_key: bool = True, clear_api_mode: bool = True,
    clear_base_url: bool = False) -> Dict[str, Any]:
    """Remove stale inline endpoint credentials from a model config.
    ``model.api_key`` is valid only for explicit custom endpoints; built-in providers resolve
    credentials from env/auth.json/the pool. Leftovers keep secrets in config.yaml and can
    contaminate later custom resolution paths."""
    if not isinstance(model_cfg, dict):
        return model_cfg
    if clear_api_key:
        model_cfg.pop("api_key", None)
        model_cfg.pop("api", None)
    if clear_api_mode:
        model_cfg.pop("api_mode", None)
    if clear_base_url:
        model_cfg.pop("base_url", None)
    return model_cfg


_MISSING = object()


def _locate_nested(config, parts: list):
    """Walk *parts* through nested dicts/lists (escape-aware, greedy-literal like ``_set_nested``).
    Returns ``(parents, container, key)`` where ``container[key]`` is the addressed leaf and
    ``parents`` lists the ``(container, key)`` hops above it, or ``None`` when any hop is missing,
    a list index is non-numeric/out of range, or a scalar is hit before the path is consumed."""
    parents = []
    current = config
    i = 0
    while True:
        remaining = parts[i:]
        if isinstance(current, list):
            try:
                key = int(remaining[0])
                current[key]
            except (TypeError, ValueError, IndexError):
                return None
            consumed = 1
        elif isinstance(current, dict):
            match = _greedy_literal_match(current, remaining)
            if match is None:
                return None
            key, consumed = match
        else:
            return None
        i += consumed
        if i == len(parts):
            return parents, current, key
        parents.append((current, key))
        current = current[key]


def _get_nested(config, dotted_key: str):
    """Return a dotted-path value (``_MISSING`` when absent); same navigation as ``_set_nested``
    so ``models.grok-4.6.context_length`` reads the real ``grok-4.6`` entry.

    Mirrors ``_set_nested``'s navigation: honors backslash-escaped dots and prefers an existing literal
    dotted key over blind splitting, so ``config get providers.p.models.grok-4.6.context_length`` reads the
    real ``grok-4.6`` entry instead of reporting the key unset (#84064).
    """
    loc = _locate_nested(config, _split_key_path(dotted_key))
    if loc is None:
        return _MISSING
    _, container, key = loc
    return container[key]


def _unset_nested(config, dotted_key: str) -> bool:
    """Remove a dotted-path value; True if it existed. Empty dict containers left behind are
    dropped, while user-authored empty lists and non-empty sibling branches are preserved.

    Same escape-aware, greedy-literal navigation as ``_set_nested`` / ``_get_nested`` (#84064): unsetting an
    unescaped dotted key removes the real literal entry rather than a phantom sibling.
    """
    loc = _locate_nested(config, _split_key_path(dotted_key))
    if loc is None:
        return False
    parents, current, key = loc
    del current[key]
    # ``parent[part] is current`` for every hop, so each now-empty dict container is dropped.
    for parent, part in reversed(parents):
        if current != {}:
            break
        del parent[part]
        current = parent
    return True


_ENV_CONFIG_KEYS = frozenset({
    'OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'VOICE_TOOLS_OPENAI_KEY',
    'EXA_API_KEY', 'PARALLEL_API_KEY', 'FIRECRAWL_API_KEY', 'FIRECRAWL_API_URL',
    'FIRECRAWL_GATEWAY_URL', 'TOOL_GATEWAY_DOMAIN', 'TOOL_GATEWAY_SCHEME',
    'TOOL_GATEWAY_USER_TOKEN', 'TAVILY_API_KEY', 'PERPLEXITY_API_KEY', 'API_SERVER_KEY',
    'BROWSERBASE_API_KEY', 'BROWSERBASE_PROJECT_ID', 'BROWSER_USE_API_KEY',
    'FAL_KEY', 'TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN',
    'TERMINAL_SSH_HOST', 'TERMINAL_SSH_USER', 'TERMINAL_SSH_KEY',
    'SUDO_PASSWORD', 'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN',
    'GITHUB_TOKEN', 'HONCHO_API_KEY'})


def _is_env_config_key(key: str) -> bool:
    """Return whether `hermes config set` routes this key to .env."""
    if "." in key:
        return False
    key_upper = key.upper()
    return (
        key_upper in _ENV_CONFIG_KEYS
        or key_upper.endswith(('_API_KEY', '_TOKEN', '_SECRET'))
        or key_upper.startswith('TERMINAL_SSH'))


def _format_config_get_value(value, *, as_json: bool) -> str:
    """Format a config value for command-line output."""
    if as_json:
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return yaml.safe_dump(value, sort_keys=False).rstrip()
    return str(value)


def get_missing_config_fields() -> List[Dict[str, Any]]:
    """Check which config fields are missing or outdated (recursive)."""
    missing = []

    def _check(defaults: dict, current: dict, prefix: str = ""):
        for key, default_value in defaults.items():
            if key.startswith('_'):
                continue
            full_key = key if not prefix else f"{prefix}.{key}"
            if key not in current:
                missing.append({"key": full_key, "default": default_value,
                                "description": f"New config option: {full_key}"})
            elif isinstance(default_value, dict) and isinstance(current.get(key), dict):
                _check(default_value, current[key], full_key)

    _check(DEFAULT_CONFIG, load_config())
    return missing


def get_missing_skill_config_vars() -> List[Dict[str, Any]]:
    """Return skill-declared config vars (``skills.config.<key>``) that are missing or empty."""
    try:
        from agent.skill_utils import discover_all_skill_config_vars, SKILL_CONFIG_PREFIX
    except Exception:
        return []

    try:
        all_vars = discover_all_skill_config_vars()
    except Exception as e:
        # A malformed SKILL.md must never break `hermes update`; this prompting is a nicety.
        logger.debug("discover_all_skill_config_vars failed: %s", e)
        return []
    if not all_vars:
        return []

    config = load_config()
    values = ((var, cfg_get(config, *f"{SKILL_CONFIG_PREFIX}.{var['key']}".split("."))) for var in all_vars)
    return [var for var, v in values if v is None or (isinstance(v, str) and not v.strip())]


def _coerce_config_version(value: Any) -> int:
    """Return a safe integer config version, treating invalid values as legacy."""
    if isinstance(value, bool):
        return 0
    try:
        version = int(value)
    except (TypeError, ValueError):
        return 0
    return max(version, 0)


def check_config_version(*, raise_on_parse_error: bool = False) -> Tuple[int, int]:
    """Return ``(current_version, latest_version)`` from the raw on-disk config.
    Reads the raw file rather than ``load_config()``: the deep-merge would make a file lacking
    ``_config_version`` inherit the latest version, hiding that the schema was never migrated.
    Invalid YAML gets a parse warning, not an automatic schema rewrite. Tolerant runtime status
    callers keep the historical latest/latest fallback for malformed YAML; mutation and explicit
    validation paths set ``raise_on_parse_error`` so a parse failure or a non-mapping root cannot
    be mistaken for an up-to-date config."""
    latest = _coerce_config_version(DEFAULT_CONFIG.get("_config_version", 1)) or 1
    config_path = get_config_path()
    if not config_path.exists():
        return latest, latest

    try:
        with open(config_path, encoding="utf-8") as f:
            config = fast_safe_load(f)
    except Exception as e:
        _warn_config_parse_failure(config_path, e)
        if raise_on_parse_error:
            raise InvalidUserConfigError(
                f"Cannot inspect {config_path}: config.yaml is not valid YAML ({e})"
            ) from e
        return latest, latest

    if config is None:
        config = {}  # empty file / bare document: valid first-run state
    if not isinstance(config, dict):
        # A list/scalar root parses fine but is just as unusable as broken YAML: save_config()
        # would refuse it later, after .env was already rewritten. Strict callers see it up front.
        if raise_on_parse_error:
            raise InvalidUserConfigError(
                f"Cannot inspect {config_path}: config.yaml top-level value must be "
                f"a mapping, got {type(config).__name__}"
            )
        config = {}
    return _coerce_config_version(config.get("_config_version")), latest


# ---- Config structure validation ----

# DEFAULT_CONFIG is the single source of truth for documented roots; the set is derived so new
# defaults are accepted automatically. These optional/legacy roots are valid on disk but
# intentionally absent from DEFAULT_CONFIG (omitted when unused / alternate schema forms).
_EXTRA_KNOWN_ROOT_KEYS = {
    "custom_providers",  # legacy list form; modern equivalent is providers: {}
    "fallback_model",    # optional single dict or chain list; omitted when disabled
    "mcp_servers",       # MCP server definitions written by setup/tools flows
    "image_gen",         # agent/image_gen_registry.py
    "video_gen",         # agent/video_gen_registry.py
    "plugins",           # plugin enable/disable lists (hermes_cli/plugins_cmd.py)
    "smart_model_routing",   # written by the setup wizard
    "platform_toolsets",     # written by the setup wizard
    "known_plugin_toolsets", # hermes_cli/tools_config.py toolset-save flow
    "known_builtin_toolsets",  # ditto — builtin toolsets a platform's checklist has offered
    "tool_gateway_declined_tools",  # per-tool Tool Gateway offer declines
    # Top-level forms read/bridged by gateway/config.py:
    "session_reset", "group_sessions_per_user", "thread_sessions_per_user",
    "stt_echo_transcripts", "reset_triggers", "always_log_local", "filter_silence_narration",
    "multiplex_profiles", "profile_routes", "platforms", "require_mention",
    "unauthorized_dm_behavior", "signal",
    "timeouts",          # unified timeout resolution section (agent/deadline.py)
}
_KNOWN_ROOT_KEYS = frozenset(DEFAULT_CONFIG.keys()) | _EXTRA_KNOWN_ROOT_KEYS

# Valid fields inside a custom_providers list entry (key_env is read at runtime by
# runtime_provider.py and auxiliary_client.py).
_VALID_CUSTOM_PROVIDER_FIELDS = {
    "name", "base_url", "api_key", "api_mode", "model", "models",
    "context_length", "rate_limit_delay", "extra_body",
    "ssl_ca_cert", "ssl_verify", "key_env"}

# Fields that look like they should be inside custom_providers, not at root
_CUSTOM_PROVIDER_LIKE_FIELDS = {"base_url", "api_key", "rate_limit_delay", "api_mode"}


@dataclass
class ConfigIssue:
    """A detected config structure problem."""
    severity: str  # "error", "warning"
    message: str
    hint: str


def _issue(issues: List["ConfigIssue"], severity: str, message: str, hint: str) -> None:
    issues.append(ConfigIssue(severity, message, hint))


def _require_fields(
    issues: List["ConfigIssue"], entry: Dict[str, Any], label: str,
    fields: Tuple[Tuple[str, str], ...], suffix: str = "") -> None:
    """Append a warning for every falsy ``field`` of *entry* (message: ``<label> is missing '<f>' field``)."""
    for field, hint in fields:
        if not entry.get(field):
            _issue(issues, "warning", f"{label} is missing '{field}' field{suffix}", hint)


_CP_REQUIRED_FIELDS = (
    ("name", "Add a name, e.g.: name: my-provider"),
    ("base_url", "Add the API endpoint URL, e.g.: base_url: https://api.example.com/v1"))
_FB_REQUIRED_FIELDS = (
    ("provider", "Add: provider: openrouter (or another provider)"),
    ("model", "Add: model: <model-name>"))
_FB_SINGLE_REQUIRED_FIELDS = (
    ("provider", "Add: provider: openrouter (or another provider)"),
    ("model", "Add: model: anthropic/claude-sonnet-4 (or another model)"))


def _validate_voice(config: Dict[str, Any], issues: List[ConfigIssue]) -> None:
    voice_cfg = config.get("voice")
    if not (isinstance(voice_cfg, dict) and "submit_mode" in voice_cfg):
        return
    submit_mode = voice_cfg.get("submit_mode")
    normalized = submit_mode.strip().lower() if isinstance(submit_mode, str) else None
    if normalized not in {"direct", "draft"}:
        _issue(issues, "error", f"voice.submit_mode must be 'direct' or 'draft', got {submit_mode!r}",
               "Set voice.submit_mode to direct (submit immediately) or draft (edit before sending)")


def _validate_entry_list(
    entries: list, label: str, issues: List[ConfigIssue], fields, *, non_dict: Tuple[str, str, str],
) -> None:
    """Validate each list entry: ``non_dict`` = (severity, message-with-{i}-and-{type}, hint) for
    non-dict items; dict items get ``_require_fields`` with *fields*."""
    severity, message, hint = non_dict
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _issue(issues, severity, message.format(i=i, type=type(entry).__name__), hint)
        else:
            _require_fields(issues, entry, f"{label}[{i}]", fields)


def _validate_custom_providers(cp: Any, issues: List[ConfigIssue]) -> None:
    """custom_providers must be a list of dicts, not a dict."""
    if isinstance(cp, dict):
        _issue(issues, "error",
               "custom_providers is a dict — it must be a YAML list (items prefixed with '-')",
               "Change to:\n  custom_providers:\n    - name: my-provider\n      base_url: https://...\n"
               "      api_key: ...")
        suspicious = set(cp.keys()) & _CUSTOM_PROVIDER_LIKE_FIELDS
        if suspicious:
            _issue(issues, "warning",
                   f"Root-level keys {sorted(suspicious)} look like custom_providers entry fields",
                   "These should be indented under a '- name: ...' list entry, not at root level")
    elif isinstance(cp, list):
        _validate_entry_list(cp, "custom_providers", issues, _CP_REQUIRED_FIELDS, non_dict=(
            "warning", "custom_providers[{i}] is not a dict (got {type})",
            "Each entry should have at minimum: name, base_url"))


def _validate_fallback_model(fb: Any, issues: List[ConfigIssue]) -> None:
    """fallback_model: single dict OR list of dicts (chain)."""
    if isinstance(fb, list):
        _validate_entry_list(fb, "fallback_model", issues, _FB_REQUIRED_FIELDS, non_dict=(
            "error", "fallback_model[{i}] should be a dict, got {type}", "Each entry needs provider + model"))
    elif not isinstance(fb, dict):
        _issue(issues, "error",
               f"fallback_model should be a dict with 'provider' and 'model', got {type(fb).__name__}",
               "Change to:\n  fallback_model:\n    provider: openrouter\n    model: anthropic/claude-sonnet-4")
    elif fb:
        _require_fields(issues, fb, "fallback_model", _FB_SINGLE_REQUIRED_FIELDS,
                        suffix=" — fallback will be disabled")


def _validate_web_backends(config: Dict[str, Any], issues: List[ConfigIssue]) -> None:
    """A stale web backend selection otherwise fails only at the first web_search/web_extract
    call with a generic "no registered provider" error; warn at startup instead."""
    # See #99199.
    web_cfg = config.get("web")
    if not isinstance(web_cfg, dict):
        return
    try:
        from tools.tool_backend_helpers import removed_backend_note
    except Exception:
        return
    seen: set = set()
    for _key in ("backend", "search_backend", "extract_backend"):
        _val = str(web_cfg.get(_key) or "").strip().lower()
        if not _val or _val in seen:
            continue
        seen.add(_val)
        note = removed_backend_note("web", _val)
        if note:
            _issue(issues, "warning",
                   f"web.{_key} is set to '{_val}', but {note} — "
                   "web_search/web_extract will fail until it is changed",
                   "Run 'hermes tools' and pick a different Web Search & Extract provider")


def validate_config_structure(config: Optional[Dict[str, Any]] = None) -> List["ConfigIssue"]:
    """Validate config.yaml structure and return detected issues (accepts a pre-loaded dict).
    Catches common YAML mistakes that otherwise surface as confusing runtime errors."""
    if config is None:
        try:
            config = load_config()
        except Exception:
            return [ConfigIssue("error", "Could not load config.yaml", "Run 'hermes setup' to create a valid config")]

    issues: List[ConfigIssue] = []
    _validate_voice(config, issues)
    cp = config.get("custom_providers")
    fb = config.get("fallback_model")
    for value, validator in ((cp, _validate_custom_providers), (fb, _validate_fallback_model)):
        if value is not None:
            validator(value, issues)

    if isinstance(cp, dict) and "fallback_model" not in config and "fallback_model" in (cp or {}):
        _issue(issues, "error", "fallback_model appears inside custom_providers instead of at root level",
               "Move fallback_model to the top level of config.yaml (no indentation)")

    if cp and not config.get("model"):
        _issue(issues, "warning",
               "custom_providers defined but no 'model' section — Hermes won't know which provider to use",
               "Add a model section:\n  model:\n    provider: custom\n    default: your-model-name\n"
               "    base_url: https://...")

    # Only provider-like fields are flagged as misplaced roots. Arbitrary unknown top-level keys
    # are deliberately NOT warned about: top-level scalars are bridged into os.environ so users
    # can feed skills/external apps env-style keys — a closed-world allowlist cannot enumerate those.
    for key in config:
        if not key.startswith("_") and key not in _KNOWN_ROOT_KEYS and key in _CUSTOM_PROVIDER_LIKE_FIELDS:
            _issue(issues, "warning",
                   f"Root-level key '{key}' looks misplaced — should it be under 'model:' or inside a 'custom_providers' entry?",
                   f"Move '{key}' under the appropriate section")

    _validate_web_backends(config, issues)
    return issues


def print_config_warnings(config: Optional[Dict[str, Any]] = None) -> None:
    """Print config structure warnings to stderr at startup; nothing if config is healthy."""
    try:
        issues = validate_config_structure(config)
    except Exception:
        issues = []
    if not issues:
        return

    lines = ["\033[33m⚠ Config issues detected in config.yaml:\033[0m"]
    for ci in issues:
        marker = "\033[31m✗\033[0m" if ci.severity == "error" else "\033[33m⚠\033[0m"
        lines.append(f"  {marker} {ci.message}")
    lines.append("  \033[2mRun 'hermes doctor' for fix suggestions.\033[0m")
    sys.stderr.write("\n".join(lines) + "\n\n")


def warn_deprecated_cwd_env_vars() -> None:
    """Warn if MESSAGING_CWD / TERMINAL_CWD is set in .env (canonical: terminal.cwd in config.yaml).
    Reads the file rather than ``os.environ`` because runtime bridges and session restoration
    legitimately set ``TERMINAL_CWD``."""
    try:
        env_map = load_env()
    except Exception:
        return

    lines: list[str] = []
    for name in ("MESSAGING_CWD", "TERMINAL_CWD"):
        val = str(env_map.get(name) or "").strip()
        if val:
            lines.append(f"  \033[33m⚠\033[0m {name}={val} found in .env — this is deprecated.")
    if lines:
        from hermes_constants import display_hermes_home

        hint_path = display_hermes_home()
        lines.insert(0, "\033[33m⚠ Deprecated .env settings detected:\033[0m")
        lines.append(
            "  \033[2mMove to config.yaml instead:  "
            "terminal:\\n    cwd: /your/project/path\033[0m")
        lines.append(f"  \033[2mThen remove the old entries from {hint_path}/.env\033[0m")
        sys.stderr.write("\n".join(lines) + "\n\n")


def _persist_migration(config: Dict[str, Any]) -> None:
    """Persist a migrated config under THE migration write invariant: a migration may only
    persist values that DIFFER from the schema default, plus explicit removals/renames of user
    data. Every migration step MUST write through here (``save_config`` with default-stripping
    ON, no ``merge_existing``) so the invariant cannot regress one migration at a time."""
    save_config(config)


def _prompt_and_save_env(name: str, info: Dict[str, Any], prompt: str, results: Dict[str, Any]) -> bool:
    """Prompt for one env var (masked when ``info['password']``), save it, record it; False if skipped."""
    value = masked_secret_prompt(prompt) if info.get("password") else line_input(prompt).strip()
    if not value:
        return False
    save_env_value(name, value)
    results["env_added"].append(name)
    print(f"  ✓ Saved {name}")
    return True


def _ask_yes_no(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    return answer in {"y", "yes"}


def migrate_config(interactive: bool = True, quiet: bool = False) -> Dict[str, Any]:
    """Migrate config to latest version, prompting for new required fields."""
    results = {"env_added": [], "config_added": [], "warnings": []}

    # Validate config.yaml before any migration side effect: sanitize_env_file() rewrites .env,
    # which must not happen when the migration will be refused for malformed YAML.
    current_ver, latest_ver = check_config_version(raise_on_parse_error=True)

    try:
        fixes = sanitize_env_file()
        if fixes and not quiet:
            print(f"  ✓ Normalized .env line formatting ({fixes} line(s) changed)")
    except Exception:
        pass  # best-effort; never block migration on sanitize failure

    # Auto-migration support floor (v12): an EXPLICIT on-disk ``_config_version`` below the
    # floor is NOT migrated and NOT rewritten — surface a message and leave the file untouched
    # (deep-merge supplies defaults at read time). A config with NO version key is a fresh
    # minimal config, not an ancient install: it gets the normal ladder and a version stamp.
    # Missing/unparseable files never trip the floor gate.
    # Imported lazily because the steps call back into this module.
    from hermes_cli.config_migrations import (
        SUPPORT_FLOOR_VERSION, run_migrations, support_floor_message)

    try:
        has_explicit_version = "_config_version" in read_user_config_raw()
    except Exception:
        has_explicit_version = False
    floor_refused = (
        has_explicit_version and current_ver < SUPPORT_FLOOR_VERSION and current_ver < latest_ver)
    if floor_refused:
        msg = support_floor_message()
        results["warnings"].append(msg)
        # stderr so it is visible even on quiet startup paths.
        sys.stderr.write(f"⚠ hermes config: {msg}\n")
        if not quiet:
            print(f"  ⚠ {msg}")
    else:
        run_migrations(current_ver, results, quiet)

    _disable_suspicious_mcp_servers(results, quiet)
    _warn_invalid_platform_toolsets(results, quiet)

    if current_ver < latest_ver and not quiet and not floor_refused:
        print(f"Config version: {current_ver} → {latest_ver}")

    missing_env = get_missing_env_vars(required_only=True)
    if missing_env and not quiet:
        print("\n⚠️  Missing required environment variables:")
        for var in missing_env:
            print(f"   • {var['name']}: {var['description']}")
    if interactive and missing_env:
        print("\nLet's configure them now:\n")
        for var in missing_env:
            if var.get("url"):
                print(f"  Get your key at: {var['url']}")
            if not _prompt_and_save_env(var["name"], var, f"  {var['prompt']}: ", results):
                results["warnings"].append(f"Skipped {var['name']} - some features may not work")
            print()

    if interactive and not quiet:
        _offer_new_optional_env_vars(current_ver, latest_ver, results)

    # New default keys are NOT materialised to disk (load_config() deep-merges DEFAULT_CONFIG at
    # read time); this list only feeds the "N new config option(s)" display.
    results["config_added"].extend(field["key"] for field in get_missing_config_fields())

    if current_ver < latest_ver and not floor_refused:
        config = read_raw_config()
        config["_config_version"] = latest_ver
        _persist_migration(config)

    missing_skill_config = get_missing_skill_config_vars()
    if missing_skill_config and interactive and not quiet:
        _offer_skill_config_vars(missing_skill_config, results)

    return results


def _disable_suspicious_mcp_servers(results: Dict[str, Any], quiet: bool) -> None:
    """Post-migration: disable exfiltration-shaped MCP stdio entries (hand-edited or from older
    installs). The stanza is preserved for auditability but marked disabled."""
    config = read_raw_config()
    # Preserve the stanza for auditability but mark it disabled so the next startup will not spawn it.
    # (#45620)
    raw_mcp_servers = config.get("mcp_servers")
    if not isinstance(raw_mcp_servers, dict):
        return
    try:
        from hermes_cli.mcp_security import validate_mcp_server_entry
    except Exception:
        return
    mcp_touched = False
    for server_name, entry in raw_mcp_servers.items():
        issues = validate_mcp_server_entry(server_name, entry) if isinstance(entry, dict) else None
        if not issues:
            continue
        entry["enabled"] = False
        mcp_touched = True
        results["warnings"].append(f"Disabled suspicious MCP server '{server_name}'")
        if not quiet:
            for issue in issues:
                print(f"  ⚠ {issue}")
            print(f"  ⚠ Disabled MCP server '{server_name}' pending review")
    if mcp_touched:
        config["mcp_servers"] = raw_mcp_servers
        _persist_migration(config)


def _warn_invalid_platform_toolsets(results: Dict[str, Any], quiet: bool) -> None:
    """Surface invalid toolset names in platform_toolsets: ``resolve_toolset()`` returns [] for an
    unknown name, silently disabling the affected tools. Best-effort; never blocks migration."""
    try:
        from toolsets import validate_toolset
        from hermes_cli.toolset_validation import validate_platform_toolsets
        from hermes_cli.toolset_scope import toolset_allowed_for_platform

        for w in validate_platform_toolsets(
                read_raw_config().get("platform_toolsets"), validate_toolset, toolset_allowed_for_platform):
            results["warnings"].append(w)
            if not quiet:
                print(f"  ⚠ {w}")
    except Exception as _ts_val_err:
        logger.debug("platform_toolsets validation skipped: %s", _ts_val_err)


def _offer_list(heading: str, items: List[str], question: str) -> bool:
    """Print a bulleted offer list and ask; False (with the "set later" hint) when declined."""
    print(heading)
    for item in items:
        print(f"    • {item}")
    print()
    if not _ask_yes_no(question):
        print("  Set later with: hermes config set <key> <value>")
        return False
    print()
    return True


def _offer_new_optional_env_vars(current_ver: int, latest_ver: int, results: Dict[str, Any]) -> None:
    """Interactively offer env vars that are NEW since the user's previous config version."""
    new_var_names: set = set()
    for ver in range(current_ver + 1, latest_ver + 1):
        new_var_names.update(ENV_VARS_BY_VERSION.get(ver, []))
    new_and_unset = [
        (name, OPTIONAL_ENV_VARS[name])
        for name in sorted(new_var_names)
        if not get_env_value(name) and name in OPTIONAL_ENV_VARS]
    if not new_and_unset or not _offer_list(
        f"\n  {len(new_and_unset)} new optional key(s) in this update:",
        [f"{name} — {info.get('description', '')}" for name, info in new_and_unset],
        "  Configure new keys? [y/N]: "):
        return
    for name, info in new_and_unset:
        print(f"  {info.get('description', name)}")
        if info.get("url"):
            print(f"  Get your key at: {info['url']}")
        _prompt_and_save_env(name, info, f"  {info.get('prompt', name)} (Enter to skip): ", results)
        print()


def _offer_skill_config_vars(missing_skill_config: List[Dict[str, Any]], results: Dict[str, Any]) -> None:
    """Prompt for skill-declared settings that are missing/empty and persist the answers."""
    if not _offer_list(
        f"\n  {len(missing_skill_config)} skill setting(s) not configured:",
        [f"{v['key']} — {v['description']} (from skill: {v.get('skill', 'unknown')})" for v in missing_skill_config],
        "  Configure skill settings? [y/N]: "):
        return
    config = read_raw_config()
    try:
        from agent.skill_utils import SKILL_CONFIG_PREFIX
    except Exception:
        SKILL_CONFIG_PREFIX = "skills.config"
    for var in missing_skill_config:
        default = var.get("default", "")
        default_hint = f" (default: {default})" if default else ""
        value = line_input(f"  {var['prompt']}{default_hint}: ").strip() or str(default or "")
        if value:
            _set_nested(config, f"{SKILL_CONFIG_PREFIX}.{var['key']}", value)
            results["config_added"].append(var["key"])
            print(f"  ✓ Saved {var['key']} = {value}")
        else:
            results["warnings"].append(
                f"Skipped {var['key']} — skill '{var.get('skill', '?')}' may ask for it later")
        print()
    _persist_migration(config)


def _merge_partial_save(raw: dict, override: dict) -> dict:
    """Merge *override* over *raw* for partial ``save_config`` writes.
    Omitted top-level sections are preserved; shared dict sections deep-merge so one nested key
    can change without dropping siblings on disk. Key REMOVALS are not supported here —
    migrations go through ``_persist_migration`` with a full ``read_raw_config()`` dict."""
    result = copy.deepcopy(override)
    for key, value in raw.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        elif isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(value, result[key])
    return result


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*: dict-over-dict recurses (so overriding one leaf
    keeps sibling defaults), and ``None`` over a dict section is ignored.

    An empty section key in config.yaml (``terminal:`` with no value) parses as YAML ``None``; treating that
    as an override would replace the entire default dict with ``None`` and crash every downstream consumer
    that expects a mapping (#58277).
    """
    result = base.copy()
    for key, value in override.items():
        over_dict = isinstance(result.get(key), dict)
        if over_dict and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif not (over_dict and value is None):
            result[key] = value
    return result


def _strip_dotted_keys(cfg: dict, dotted_keys: set) -> Tuple[dict, set]:
    """Remove dotted leaf keys from *cfg* in place -> ``(cfg, keys_actually_present)``.
    ``save_config`` drops managed-scope leaves this way so a bulk write never persists a user
    value that would lose to the managed layer on the next load."""
    stripped: set = set()
    for dotted in dotted_keys:
        *parents, leaf = dotted.split(".")
        node = cfg_get(cfg, *parents)
        if isinstance(node, dict) and leaf in node:
            del node[leaf]
            stripped.add(dotted)
    return cfg, stripped


_ENV_REF_RE = re.compile(r"\${([^}]+)}")


def _env_ref_lookup(name: str) -> Optional[str]:
    """Resolve the env var behind a ``${VAR}`` / ``${env:VAR}`` ref — plain ``os.environ`` outside
    a profile secret scope (legacy behavior for the default profile).

    Inside a scope (a multiplexed gateway turn, a secondary profile's config load, a cron job) the read goes
    through ``agent.secret_scope.get_secret`` so the ref resolves against *that* profile's ``.env``: under
    multiplexing a miss is a miss, never another profile's ``os.environ`` value (#84079 — every profile
    "had" the default profile's ``${MATRIX_ACCESS_TOKEN}`` and fanned out). Same policy as
    ``gateway.config._getenv`` and ``get_env_value``.
    """
    try:
        from agent.secret_scope import current_secret_scope, get_secret as _get_secret
    except Exception:
        return os.environ.get(name)
    if current_secret_scope() is None:
        return os.environ.get(name)
    return _get_secret(name)


def _env_expand_match(m: re.Match) -> str:
    """Expand one ``${VAR}`` (legacy bare name) or ``${env:VAR}`` (Cursor-style SecretRef).
    Other SecretRef sources (``file:``, ``bitwarden:``, ``vault:``...) are NOT resolved here:
    external backends inject their values into the environment at startup (the ``secrets:``
    block), so a config ref only ever needs the env shape. Unresolved refs stay verbatim so
    callers can detect them."""
    raw = m.group(0)
    inner = m.group(1).strip()
    name = _env_ref_var_name(inner)
    if name is None:
        if not inner.startswith("env:") and _is_non_env_secret_ref(inner):
            logger.warning(
                "Config ref %r uses source %r which is not resolvable in "
                "config.yaml — external secret sources inject env vars at "
                "startup, so reference the variable as ${env:NAME} instead",
                raw, inner.split(":", 1)[0])
        return raw  # non-env source, or empty ``${env:}``
    val = _env_ref_lookup(name)
    if val is not None:
        return val
    if inner.startswith("env:"):
        logger.warning(
            "Config ref %r: %s is not set (check ~/.hermes/.env); "
            "keeping the literal placeholder", raw, name)
    return raw


def _is_non_env_secret_ref(ref: str) -> bool:
    """True for a SecretRef body with a non-``env`` source (``bitwarden:FOO``, ``vault:...``)."""
    return ":" in ref and re.match(r"^[a-z][a-z0-9_-]*:", ref) is not None


def _env_ref_var_name(ref: str) -> Optional[str]:
    """Env-var name a ``${...}`` body reads, or None for a non-env source / empty ``env:``."""
    ref = ref.strip()
    if ref.startswith("env:"):
        return ref[len("env:"):].strip() or None
    if _is_non_env_secret_ref(ref):
        return None
    return ref


def _expand_env_vars(obj):
    """Recursively expand ``${VAR}`` / ``${env:VAR}`` in string values (keys/non-strings untouched)."""
    if isinstance(obj, str):
        return _ENV_REF_RE.sub(_env_expand_match, obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


def _env_ref_snapshot(obj, snapshot=None):
    """Map each env-sourced ``${...}`` ref in *obj* to its current value.
    Stored with cached ``load_config()`` results so a cache hit can detect that the expansion was
    made against a different environment (load before ``load_hermes_dotenv()``, in-process
    rotation) — file mtime/size alone cannot see either.

    See #58514.
    """
    if snapshot is None:
        snapshot = {}
    if isinstance(obj, str):
        for raw in _ENV_REF_RE.findall(obj):
            name = _env_ref_var_name(raw)
            if name is not None:
                snapshot[name] = _env_ref_lookup(name)
    elif isinstance(obj, dict):
        for value in obj.values():
            _env_ref_snapshot(value, snapshot)
    elif isinstance(obj, list):
        for item in obj:
            _env_ref_snapshot(item, snapshot)
    return snapshot


def _items_by_unique_name(items):
    """Return a name-indexed dict only when all items have unique string names."""
    if not isinstance(items, list):
        return None
    indexed = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None
        name = item["name"]
        if name in indexed:
            return None
        indexed[name] = item
    return indexed


def _preserve_env_ref_templates(current, raw, loaded_expanded=None):
    """Restore raw ``${VAR}`` templates where the value is otherwise unchanged, so persisting a
    loaded (expanded) config never writes the plaintext secret back to ``config.yaml``."""
    if isinstance(current, str) and isinstance(raw, str) and _ENV_REF_RE.search(raw):
        if current in (raw, loaded_expanded) or _expand_env_vars(raw) == current:
            return raw
        return current

    if isinstance(current, dict) and isinstance(raw, dict):
        return {
            key: _preserve_env_ref_templates(
                value, raw.get(key),
                loaded_expanded.get(key) if isinstance(loaded_expanded, dict) else None)
            for key, value in current.items()}

    if isinstance(current, list) and isinstance(raw, list):
        # Match named objects (e.g. custom_providers) by name so reordering keeps templates;
        # with duplicate names fall back to positional matching rather than shadowing an entry.
        current_by_name = _items_by_unique_name(current)
        raw_by_name = _items_by_unique_name(raw)
        loaded_by_name = _items_by_unique_name(loaded_expanded)
        if current_by_name is not None and raw_by_name is not None:
            return [
                _preserve_env_ref_templates(
                    item, raw_by_name.get(item.get("name")),
                    loaded_by_name.get(item.get("name")) if loaded_by_name is not None else None)
                for item in current]
        return [
            _preserve_env_ref_templates(
                item,
                raw[index] if index < len(raw) else None,
                loaded_expanded[index]
                if isinstance(loaded_expanded, list) and index < len(loaded_expanded)
                else None)
            for index, item in enumerate(current)]

    return current


def _explicit_config_paths(config: Dict[str, Any]) -> Set[Tuple[str, ...]]:
    """Leaf paths explicitly present in a RAW (un-normalized) config, so values injected by
    normalisation are never mistaken for user-set ones. Feeds ``_strip_default_values``."""
    paths: Set[Tuple[str, ...]] = set()

    def _walk(value: Any, path: Tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                _walk(child, path + (key,))
        elif path:
            paths.add(path)

    _walk(config, ())
    return paths


def _strip_default_values(
    config: Dict[str, Any], defaults: Dict[str, Any] = DEFAULT_CONFIG,
    preserve_keys: Optional[Set[Tuple[str, ...]]] = None) -> Dict[str, Any]:
    """Return *config* without keys whose values match *defaults*.
    Paths in *preserve_keys* (explicitly present in the user's raw config) are always kept even
    when equal to the default. Dicts whose every child is stripped are removed entirely so
    default-only subtrees never bloat ``config.yaml``."""
    preserve_keys = {("_config_version",)} | set(preserve_keys or ())

    def _strip(value: Any, default: Any, path: Tuple[str, ...]) -> Any:
        if path in preserve_keys:
            return copy.deepcopy(value)
        if isinstance(value, dict) and value:
            default_dict = default if isinstance(default, dict) else {}
            stripped = {k: _strip(v, default_dict.get(k), path + (k,)) for k, v in value.items()}
            return {k: v for k, v in stripped.items() if v is not None} or None
        return None if value == default else copy.deepcopy(value)

    return _strip(config, defaults, ()) or {}


def split_model_config_default(raw_default: Any) -> tuple[str, str]:
    """Canonicalize ``model.default``/``model.model`` -> ``(model, provider)``; a dict value pairs
    the model string with the provider it must be routed through."""
    if isinstance(raw_default, dict):
        provider = str(raw_default.get("provider") or "").strip()
        model = raw_default.get("model") or raw_default.get("default")
        return (str(model or "").strip(), provider)
    return (str(raw_default or "").strip(), "")


def _normalize_root_model_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    """Canonicalize the ``model`` section at the single load/save chokepoint.
    Root-level ``provider``/``base_url``/``context_length`` (older layouts) are moved under
    ``model`` only when the corresponding ``model.*`` key is empty — never overriding. ``api_base``
    (the OpenAI-SDK/LiteLLM name users reach for) is an alias for ``base_url``; the runtime reads
    only ``model.base_url``. A dict-valued ``default``/``model``/``name`` is flattened so no reader
    sees a nested dict, and the id is canonicalized to ``default``.

    Also aliases ``api_base`` → ``base_url`` (issue #8919). ``api_base`` is the intuitive name OpenAI-SDK /
    LiteLLM users reach for, and ``hermes config set`` blindly accepts any dotted key — so
    ``model.api_base`` got written, confirmed, and then silently ignored by the runtime resolver (which
    reads only ``model.base_url``), causing requests to fall back to OpenRouter. We migrate the alias to the
    canonical key (fallback-only — never override an explicit ``base_url``) and drop the alias so it can't
    confuse later loads.
    Finally, canonicalizes the model-id key to ``model.default`` (issue #34500). The runtime resolver and
    ~14 other readers select the chat model via ``model.default``; ``model.model`` was already aliased
    inline at some sites but ``model.name`` was not, so a custom-provider config like ``model: {name: <id>,
    provider: <custom>}`` resolved to an empty model and the API request went out with ``model=`` (HTTP 400
    from OpenAI-compatible backends) — while display paths (``hermes status``/``dump``) read ``name`` and
    *showed* the model, making the failure silent. Normalizing here (the single load/save chokepoint) means
    every reader, present and future, sees a populated ``default`` and the stale alias is migrated out of
    config.yaml on the next save. Precedence: ``default`` > ``model`` > ``name`` (never overrides an
    explicit ``default``, so existing configs are unaffected).
    """
    model_in = config.get("model")
    needs_model_work = isinstance(model_in, dict) and (
        model_in.get("api_base")
        or model_in.get("model") or model_in.get("name")
        or any(isinstance(model_in.get(k), dict) for k in ("default", "model", "name")))
    has_root = any(config.get(k) for k in ("provider", "base_url", "context_length", "api_base"))
    if not has_root and not needs_model_work:
        return config

    config = dict(config)
    model = config.get("model")
    model = dict(model) if isinstance(model, dict) else {"default": model} if model else {}
    config["model"] = model

    # Flatten ``{provider: <p>, model: <m>}``. The nested provider wins over the merged default
    # ``"auto"`` (which runtime resolution treats as authoritative) but never over a configured one.
    for _key in ("default", "model", "name"):
        _val = model.get(_key)
        if isinstance(_val, dict):
            _nested_model = _val.get("model") or _val.get("default")
            _nested_provider = str(_val.get("provider") or "").strip()
            model[_key] = str(_nested_model or "").strip()
            if _nested_provider:
                _outer_provider = str(model.get("provider") or "").strip()
                if not _outer_provider or _outer_provider == "auto":
                    model["provider"] = _nested_provider

    for key in ("provider", "base_url", "context_length"):
        root_val = config.get(key)
        if root_val and not model.get(key):
            model[key] = root_val
        config.pop(key, None)

    for alias_val in (config.get("api_base"), model.get("api_base")):
        if alias_val and not model.get("base_url"):
            model["base_url"] = alias_val
    config.pop("api_base", None)
    model.pop("api_base", None)

    # ``model``/``name`` are last-resort aliases (in that order), then dropped.
    alias = model.get("model") or model.get("name")
    if not model.get("default") and alias:
        model["default"] = alias
    if model.get("default"):
        model.pop("model", None)
        model.pop("name", None)

    return config


def _normalize_max_turns_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Move legacy root-level ``max_turns`` under ``agent``; the schema default is injected only
    when the user set max_turns somewhere (so save_config can otherwise omit it)."""
    config = dict(config)
    agent_config = dict(config.get("agent") or {})
    if "max_turns" in config and "max_turns" not in agent_config:
        agent_config["max_turns"] = config["max_turns"]
    config["agent"] = agent_config
    config.pop("max_turns", None)
    return config


def _canonicalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """The load/save normalization pipeline: max_turns relocation, then model-section canon."""
    return _normalize_root_model_keys(_normalize_max_turns_config(config))


# Sentinel for an unlimited turn budget. ``sys.maxsize`` survives the str->int round-trip through
# the HERMES_MAX_ITERATIONS env bridge, works in every ``<``/``>=``/``max - used`` comparison in
# the iteration budget without an "unlimited" special case, and is unreachable in practice.
TURN_LIMIT_UNLIMITED = sys.maxsize

# Spellings that mean "no limit" (compared lowercased, whitespace-stripped).
_UNLIMITED_SPELLINGS = frozenset({
    "none", "null", "unlimited", "infinite", "infinity", "inf", "∞", "-1", "0"})


def resolve_turn_limit(raw: Any, default: int = TURN_LIMIT_UNLIMITED) -> int:
    """Normalize a raw ``agent.max_turns`` value into an int iteration cap (always >= 1)."""
    # bool is a subclass of int; reject explicitly so True/False don't become 1/0.
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        n = int(raw)
    elif isinstance(raw, str):
        s = raw.strip().lower()
        if not s:
            return default
        if s in _UNLIMITED_SPELLINGS:
            return TURN_LIMIT_UNLIMITED
        try:
            n = int(s)
        except ValueError:
            try:
                n = int(float(s))
            except ValueError:
                logger.debug("resolve_turn_limit: unparseable value %r → default %d", raw, default)
                return default
    else:
        # Unknown type (list, dict, …) — don't crash the agent over a bad config.
        logger.debug("resolve_turn_limit: unsupported type %s (%r) → default %d", type(raw).__name__, raw, default)
        return default
    return TURN_LIMIT_UNLIMITED if n <= 0 else n


def cfg_get(cfg: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    """Traverse nested dict keys safely, returning ``default`` on any miss.
    Explicit ``None`` values are returned as-is (``dict.get`` semantics: ``default`` only when the
    key is absent). Named ``cfg_get`` to avoid shadowing the ubiquitous ``cfg_path`` local."""
    if not isinstance(cfg, dict):
        return default
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _read_raw_config_impl(*, want_deepcopy: bool) -> Dict[str, Any]:
    with _CONFIG_LOCK:
        try:
            config_path = get_config_path()
            st = config_path.stat()
            cache_key = (st.st_mtime_ns, st.st_size)
        except (FileNotFoundError, OSError):
            return {}

        path_key = str(config_path)
        cached = _RAW_CONFIG_CACHE.get(path_key)
        if cached is not None and cached[:2] == cache_key:
            return copy.deepcopy(cached[2]) if want_deepcopy else cached[2]

        try:
            with open(config_path, encoding="utf-8") as f:
                data = fast_safe_load(f) or {}
        except Exception as e:
            _warn_config_parse_failure(config_path, e)
            return {}

        if not isinstance(data, dict):
            data = {}
        # The cache stores its own deepcopy. The readonly path returns THAT object (identity
        # invariant: later cache hits return the same dict); the mutable path returns the parse.
        cached_copy = copy.deepcopy(data)
        _RAW_CONFIG_CACHE[path_key] = (cache_key[0], cache_key[1], cached_copy)
        return data if want_deepcopy else cached_copy


def read_raw_config() -> Dict[str, Any]:
    """Read config.yaml as-is (no defaults merged, no migration); ``{}`` if missing/unparseable.
    Cached on (mtime_ns, size); returns a deepcopy since callers mutate before ``save_config()``."""
    return _read_raw_config_impl(want_deepcopy=True)


def read_user_config_raw(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Read a user ``config.yaml`` EXACTLY as written (no defaults/overlay/expansion, no cache).
    ONLY legal for write-back round-trips and raw-file diagnostics — behavioral reads must use
    load_config()/load_config_readonly()."""
    if config_path is None:
        config_path = get_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            data = fast_safe_load(f) or {}
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def read_raw_config_readonly() -> Dict[str, Any]:
    """``read_raw_config()`` without the per-call deepcopy, for callers that ONLY READ.
    **Mutating the result corrupts the in-process cache for every subsequent caller.** Meant for
    per-turn policy checks that were paying a full config deepcopy 2-3x per agent turn."""
    return _read_raw_config_impl(want_deepcopy=False)


def _refuse_overwrite(config_path: Path, reason: str, exc: Exception, fix: str) -> RuntimeError:
    return RuntimeError(f"Refusing to overwrite {config_path}: existing config.yaml {reason} ({exc}). {fix}")


_FIX_PERMS = "Fix the file permissions or move it aside first."
_FIX_YAML = "Fix the file or restore from a .corrupt.*.bak backup first."


def require_readable_config_before_write(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Refuse to replace an existing config.yaml that cannot be read or parsed; return the mapping.
    Guards two collapse-to-empty failure modes that would let a read-then-write caller silently
    wipe user overrides: an unreadable file (permissions / broken mount) and an unparseable or
    non-mapping root — bare-``except`` loaders treat both as ``{}``, so a subsequent write would
    replace the recoverable file with only the caller's partial dict. Fails closed."""
    if config_path is None:
        config_path = get_config_path()
    try:
        config_path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise _refuse_overwrite(config_path, "cannot be accessed", exc, _FIX_PERMS) from exc

    try:
        with open(config_path, encoding="utf-8") as f:
            loaded = fast_safe_load(f)
    except OSError as exc:
        raise _refuse_overwrite(config_path, "cannot be read", exc, _FIX_PERMS) from exc
    except Exception as exc:
        _warn_config_parse_failure(config_path, exc, fallback="refuse-write")
        raise _refuse_overwrite(config_path, "is not valid YAML", exc, _FIX_YAML) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        exc = TypeError(f"top-level YAML must be a mapping, got {type(loaded).__name__}")
        _warn_config_parse_failure(config_path, exc, fallback="refuse-write")
        raise RuntimeError(
            f"Refusing to overwrite {config_path}: top-level YAML must be a mapping, got "
            f"{type(loaded).__name__}. Fix the file or restore from a .corrupt.*.bak backup first."
        ) from exc
    return loaded


def atomic_config_write(config_path: Path, data: Any, **kwargs: Any) -> None:
    """Fail-closed atomic write for ``config.yaml`` (``require_readable_config_before_write`` first)."""
    require_readable_config_before_write(config_path)
    atomic_yaml_write(config_path, data, **kwargs)


def load_config() -> Dict[str, Any]:
    """Load the merged configuration (DEFAULT_CONFIG + config.yaml + managed scope, env-expanded).
    Cached on the file signature; returns a deepcopy since most call sites mutate the result.
    Read-only hot paths should use ``load_config_readonly()`` to skip the deepcopy."""
    return _load_config_impl(want_deepcopy=True)


def load_config_readonly() -> Dict[str, Any]:
    """``load_config()`` without the defensive deepcopy (~half of the 265us cache-hit cost).
    **Mutating the returned dict (or any nested structure) corrupts the in-process cache for
    every subsequent caller** — only for code paths that never write to the result."""
    return _load_config_impl(want_deepcopy=False)


def _ensure_dict(parent: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return ``parent[key]`` as a dict, replacing a missing or non-dict value with ``{}``."""
    child = parent.get(key)
    if not isinstance(child, dict):
        child = {}
        parent[key] = child
    return child


def write_platform_config_field(
    platform_key: str, field_key: str, value: Any, *, raw: bool = False) -> None:
    """Persist one scalar field under ``platforms.<platform_key>``.
    ``raw=True`` (CLI setup flows) edits only the user's raw file; dashboard routes use the
    default loaded-config path to keep their profile-scoped ``load_config`` behavior."""
    config = read_raw_config() if raw else load_config()
    platforms = _ensure_dict(config, "platforms")
    _ensure_dict(platforms, platform_key)[field_key] = value
    save_config(config)


# ``terminal.<key>`` -> env var read by tools.terminal_tool. Every key maps to ``TERMINAL_<KEY>``
# except ``backend`` (historically ``TERMINAL_ENV``).
TERMINAL_CONFIG_ENV_MAP = {
    "backend": "TERMINAL_ENV",
    **{
        key: f"TERMINAL_{key.upper()}"
        for key in (
            "modal_mode", "degraded_mode", "cwd", "temp_dir", "timeout", "lifetime_seconds",
            "docker_image", "docker_forward_env", "singularity_image", "modal_image",
            "daytona_image", "vercel_runtime", "ssh_host", "ssh_user", "ssh_port", "ssh_key",
            "container_cpu", "container_memory", "container_disk", "container_persistent",
            "docker_volumes", "docker_env", "docker_mount_cwd_to_workspace", "docker_network",
            "docker_extra_args", "docker_shm_size", "docker_run_as_host_user",
            "docker_persist_across_processes", "docker_shared_container_key",
            "docker_orphan_reaper", "sandbox_dir", "persistent_shell")}}


def _terminal_env_value(value: Any) -> str:
    return json.dumps(value) if isinstance(value, (list, dict)) else str(value)


def _terminal_config_value_is_bridgeable(key: str, value: Any) -> bool:
    """Return whether a terminal config value owns its mirrored env var."""
    return not (key == "cwd" and str(value or "").strip() in {".", "auto", "cwd"})


def terminal_config_owned_env_vars(terminal_config: Any) -> Set[str]:
    """Return env vars explicitly owned by a raw ``terminal`` config section."""
    if not isinstance(terminal_config, dict):
        return set()
    return {
        env_var
        for key, env_var in TERMINAL_CONFIG_ENV_MAP.items()
        if key in terminal_config
        and _terminal_config_value_is_bridgeable(key, terminal_config[key])}


def terminal_config_env_var_for_key(key: str) -> Optional[str]:
    """Return the env var mirrored by a ``terminal.*`` config key."""
    return TERMINAL_CONFIG_ENV_MAP.get(key[len("terminal."):]) if key.startswith("terminal.") else None


def _is_ssh_remote_tilde_cwd(backend: str, cwd: str) -> bool:
    """Whether the remote SSH shell must expand *cwd* itself: ``~`` expanded on the Hermes host
    would name the host/container home instead of the SSH user's."""
    return (backend or "").strip().lower() == "ssh" and (cwd == "~" or cwd.startswith("~/"))


def apply_terminal_config_to_env(
    *, env: Optional[Dict[str, str]] = None, config: Optional[Dict[str, Any]] = None,
    override: Optional[bool] = None) -> Dict[str, str]:
    """Bridge ``terminal.*`` config into the env vars terminal tools read.
    ``tools.terminal_tool`` is environment-driven because it also runs in child processes (TUI,
    dashboard PTY, gateway workers); this gives those launch paths the same bridge as the CLI
    without importing ``cli.py``. Explicit keys in the user's raw ``terminal`` section override
    matching env values; merged defaults only backfill missing env vars."""
    target = os.environ if env is None else env

    raw_terminal_cfg = read_raw_config().get("terminal")
    file_has_terminal_config = isinstance(raw_terminal_cfg, dict)
    raw_terminal_cfg = raw_terminal_cfg if file_has_terminal_config else {}
    should_override = file_has_terminal_config if override is None else override

    cfg = config if config is not None else load_config_readonly()
    terminal_cfg = cfg.get("terminal", {}) if isinstance(cfg, dict) else {}
    if not isinstance(terminal_cfg, dict):
        return target

    # A caller-supplied config is its own source of explicit keys; otherwise only keys present
    # in raw config.yaml may override existing env values (DEFAULT_CONFIG keys are backfill-only).
    explicit_keys = terminal_cfg.keys() if config is not None else raw_terminal_cfg.keys()
    backend_sources = (terminal_cfg.get("backend"), target.get("TERMINAL_ENV"))
    if not (config is not None or "backend" in raw_terminal_cfg):
        backend_sources = backend_sources[::-1]  # env wins when the file did not set backend
    terminal_backend = str(backend_sources[0] or backend_sources[1] or "")

    for cfg_key, env_var in TERMINAL_CONFIG_ENV_MAP.items():
        if cfg_key not in terminal_cfg:
            continue
        value = terminal_cfg[cfg_key]
        if not _terminal_config_value_is_bridgeable(cfg_key, value):
            continue
        if cfg_key == "cwd":
            raw_cwd = str(value or "").strip()
            if isinstance(value, str) and not _is_ssh_remote_tilde_cwd(terminal_backend, raw_cwd):
                value = os.path.expanduser(value)
        if (should_override and cfg_key in explicit_keys) or env_var not in target:
            target[env_var] = _terminal_env_value(value)
    return target


def _load_config_cache_sig(config_path: Path) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int, int, int]]]:
    """Return ``(user_sig, cache_sig)`` for ``_LOAD_CONFIG_CACHE``.
    The managed config file's (mtime, size) is folded in ((0, 0) = none) so editing it invalidates
    the merged result. ``cache_sig`` is None only when neither file exists (nothing to cache on)."""
    try:
        st = config_path.stat()
        user_sig: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        user_sig = None
    managed_dir = managed_scope.get_managed_dir()
    try:
        mst = (managed_dir / "config.yaml").stat() if managed_dir else None
        managed_sig = (mst.st_mtime_ns, mst.st_size) if mst else (0, 0)
    except OSError:
        managed_sig = (0, 0)
    if user_sig is None and managed_sig == (0, 0):
        return None, None
    return user_sig, (*(user_sig or (0, 0)), *managed_sig)


def _last_known_good_fallback(config_path: Path, path_key: str, cache_sig, exc: Exception) -> Optional[Dict[str, Any]]:
    """Warn about a parse failure and return the last-known-good config, or None (-> defaults).
    A parse failure must not silently replace the effective config with defaults — that drops
    EVERY user override, including security-critical ``approvals.deny`` rules, when a gateway
    user mid-edits config.yaml into broken YAML. Keep serving the last good config until fixed."""
    # Falling through to DEFAULT_CONFIG here drops EVERY user override — including security-critical
    # ``approvals.deny`` rules, which are supposed to block commands even under yolo. Within a running
    # process we still have the last successfully loaded config — keep serving it until the file is fixed.
    # See #31188.
    lkg = _LAST_EXPANDED_CONFIG_BY_PATH.get(path_key)
    _warn_config_parse_failure(
        config_path, exc, fallback="last-known-good" if lkg is not None else "defaults")
    if lkg is None:
        return None
    # save_config() stores the pre-expansion dict (templates preserved); the load path stores the
    # expanded one. Expand defensively — idempotent when already expanded.
    lkg_copy: Dict[str, Any] = _expand_env_vars(copy.deepcopy(lkg))
    if cache_sig is not None:
        # Cache under the corrupt file's signature (empty env snapshot: always valid) so repeated
        # loads don't re-parse; fixing the file changes the signature and reloads normally.
        _LOAD_CONFIG_CACHE[path_key] = (*cache_sig, lkg_copy, {})
    return lkg_copy


def _merge_managed_overlay(expanded: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
    """Apply the managed-scope overlay; returns ``(merged, managed_config_or_falsy)``.
    Managed wins at the leaf and is applied AFTER user expansion so a user ``${VAR}`` cannot shadow
    a managed literal: managed values expand only against the process environment. This
    deliberately inverts the usual env-over-config precedence for the keys the managed layer pins
    (docs/design/managed-scope.md §4.1)."""
    managed_config = managed_scope.load_managed_config()
    if not managed_config:
        return expanded, managed_config
    # Same canonicalization as the user config BEFORE merging (parity with
    # managed_scope.apply_managed_overlay) so the merged result never exposes a nested dict.
    managed_normalized = _normalize_root_model_keys(managed_config)
    if isinstance(managed_normalized.get("model"), str):
        managed_normalized = dict(managed_normalized)
        managed_normalized["model"] = {"default": managed_normalized["model"]}
    return _deep_merge(expanded, _expand_env_vars(managed_normalized)), managed_config


def _load_config_impl(*, want_deepcopy: bool) -> Dict[str, Any]:
    with _CONFIG_LOCK:
        ensure_hermes_home()
        config_path = get_config_path()
        path_key = str(config_path)

        user_sig, cache_sig = _load_config_cache_sig(config_path)

        cached = _LOAD_CONFIG_CACHE.get(path_key)
        if cached is not None and cache_sig is not None and cached[:4] == cache_sig:
            # Signatures match, but the cached expansion is only valid if every ${VAR} it was
            # expanded against still has the same value — otherwise a load before
            # load_hermes_dotenv() pins unexpanded literals for the process lifetime.
            # Without this, a load_config() that ran before load_hermes_dotenv() pins unexpanded literals
            # (e.g. auxiliary.<task>.api_key) for the life of the process (#58514).
            env_snapshot = cached[5] if len(cached) > 5 else {}
            if all(_env_ref_lookup(k) == v for k, v in env_snapshot.items()):
                return copy.deepcopy(cached[4]) if want_deepcopy else cached[4]

        config = copy.deepcopy(DEFAULT_CONFIG)

        if user_sig is not None:
            try:
                with open(config_path, encoding="utf-8") as f:
                    user_config = fast_safe_load(f) or {}

                if "max_turns" in user_config:
                    agent_user_config = dict(user_config.get("agent") or {})
                    if agent_user_config.get("max_turns") is None:
                        agent_user_config["max_turns"] = user_config["max_turns"]
                    user_config["agent"] = agent_user_config
                    user_config.pop("max_turns", None)

                config = _deep_merge(config, user_config)
            except Exception as e:
                lkg_copy = _last_known_good_fallback(config_path, path_key, cache_sig, e)
                if lkg_copy is not None:
                    return copy.deepcopy(lkg_copy) if want_deepcopy else lkg_copy

        normalized = _canonicalize_config(config)
        expanded, managed_config = _merge_managed_overlay(_expand_env_vars(normalized))
        _LAST_EXPANDED_CONFIG_BY_PATH[path_key] = copy.deepcopy(expanded)
        if cache_sig is not None:
            # The cache stores its own deepcopy so load_config() callers can mutate freely while
            # load_config_readonly() callers all see the same stable object. The env snapshot
            # records the values this expansion was made against so later loads detect drift.
            cached_copy = copy.deepcopy(expanded)
            env_snapshot = _env_ref_snapshot(normalized)
            if managed_config:
                _env_ref_snapshot(managed_config, env_snapshot)
            _LOAD_CONFIG_CACHE[path_key] = (*cache_sig, cached_copy, env_snapshot)
            # Readonly path returns the same object later calls will see (identity invariant).
            if not want_deepcopy:
                return cached_copy
        else:
            _LOAD_CONFIG_CACHE.pop(path_key, None)
        # First-load result is a fresh dict (not aliased to the cache); safe to return directly.
        return expanded


_SECURITY_COMMENT = """
# ── Security ──────────────────────────────────────────────────────────
# Secret redaction is ON by default — strings that look like API keys,
# tokens, and passwords are masked in tool output, logs, and chat
# responses before the model or user ever sees them. Set redact_secrets
# to false to disable (e.g. when developing the redactor itself).
# tirith pre-exec scanning is enabled by default when the tirith binary
# is available. Configure via security.tirith_* keys or env vars
# (TIRITH_ENABLED, TIRITH_BIN, TIRITH_TIMEOUT, TIRITH_FAIL_OPEN).
#
# security:
#   redact_secrets: true
#   tirith_enabled: true
#   tirith_path: "tirith"
#   tirith_timeout: 5
#   tirith_fail_open: true
"""

_FALLBACK_COMMENT = """
# ── Fallback Model ────────────────────────────────────────────────────
# Automatic provider failover when primary is unavailable.
# Uncomment and configure to enable. Triggers on rate limits (429),
# overload (529), service errors (503), or connection failures.
#
# Supported providers:
#   openrouter   (OPENROUTER_API_KEY)  — routes to any model
#   openai-codex (OAuth — hermes auth) — OpenAI Codex
#   nous         (OAuth — hermes auth) — Nous Portal
#   zai          (ZAI_API_KEY)         — Z.AI / GLM
#   kimi-coding  (KIMI_API_KEY)        — Kimi / Moonshot
#   kimi-coding-cn (KIMI_CN_API_KEY)   — Kimi / Moonshot (China)
#   minimax      (MINIMAX_API_KEY)     — MiniMax
#   minimax-cn   (MINIMAX_CN_API_KEY)  — MiniMax (China)
#   bedrock      (AWS IAM / boto3)     — AWS Bedrock (Converse API)
#
# For custom OpenAI-compatible endpoints, add base_url and key_env.
#
# fallback_model:
#   provider: openrouter
#   model: anthropic/claude-sonnet-4
"""


def _strip_managed_keys_for_save(config: Dict[str, Any]) -> Dict[str, Any]:
    """Drop every leaf the managed layer pins (bulk safety net; single-key ``config set``
    hard-rejects) and tell the user what was not saved."""
    managed_keys = managed_scope.managed_config_keys()
    if not managed_keys:
        return config
    config, _stripped = _strip_dotted_keys(copy.deepcopy(config), managed_keys)
    if _stripped:
        print(
            f"Note: {len(_stripped)} managed setting(s) were not saved "
            f"(managed by your administrator): {', '.join(sorted(_stripped))}", file=sys.stderr)
    return config


def _commented_sections_for_save(normalized: Dict[str, Any]) -> Optional[str]:
    """Commented-out example blocks for features that are off/unconfigured."""
    parts = []
    if (normalized.get("security") or {}).get("redact_secrets") is None:
        parts.append(_SECURITY_COMMENT)
    fb = normalized.get("fallback_model", {})
    fb_entries = fb if isinstance(fb, list) else [fb]
    if not any(isinstance(e, dict) and e.get("provider") and e.get("model") for e in fb_entries):
        parts.append(_FALLBACK_COMMENT)
    return "".join(parts) or None


def save_config(
    config: Dict[str, Any], *, strip_defaults: bool = True,
    preserve_keys: Optional[Set[Tuple[str, ...]]] = None, merge_existing: bool = False):
    """Save configuration to ~/.hermes/config.yaml.
    Schema defaults are not written unless the user explicitly set them (the path exists in the
    raw config before normalisation), so config.yaml is never contaminated with defaults that
    would hide future default changes. ``merge_existing`` deep-merges the on-disk raw config
    under *config* so partial callers cannot drop sections they omitted."""
    with _CONFIG_LOCK:
        if is_managed():
            managed_error("save configuration")
            return

        config = _strip_managed_keys_for_save(config)

        ensure_hermes_home()
        config_path = get_config_path()
        require_readable_config_before_write(config_path)
        # Explicit user paths come from the RAW dict BEFORE normalisation (which may inject
        # agent.max_turns) so _strip_default_values keeps exactly what the user set.
        _raw_for_paths = read_raw_config()
        if merge_existing and _raw_for_paths:
            config = _merge_partial_save(_raw_for_paths, config)

        current_normalized = _canonicalize_config(config)
        normalized = current_normalized
        if _raw_for_paths:
            normalized = _preserve_env_ref_templates(
                normalized, _canonicalize_config(_raw_for_paths),
                _LAST_EXPANDED_CONFIG_BY_PATH.get(str(config_path)))

        if strip_defaults:
            # ``_strip_default_values`` always preserves ``_config_version`` itself.
            effective_preserve_keys = _explicit_config_paths(_raw_for_paths) | set(preserve_keys or ())
            normalized = _strip_default_values(normalized, DEFAULT_CONFIG, preserve_keys=effective_preserve_keys)

        atomic_yaml_write(config_path, normalized, extra_content=_commented_sections_for_save(normalized))
        _secure_file(config_path)
        _RAW_CONFIG_CACHE.pop(str(config_path), None)
        _LAST_EXPANDED_CONFIG_BY_PATH[str(config_path)] = copy.deepcopy(current_normalized)


def _parse_env_value(raw_value: str) -> str:
    """Parse the small .env value subset Hermes writes itself (bare, 'single', or "double" with
    ``\\"`` / ``\\\\`` escapes)."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        quoted = value[1:-1]
        parsed: list[str] = []
        i = 0
        while i < len(quoted):
            escaped = quoted[i] == "\\" and quoted[i + 1:i + 2] in ('"', "\\")
            parsed.append(quoted[i + 1] if escaped else quoted[i])
            i += 2 if escaped else 1
        return "".join(parsed)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


# load_env() memo keyed on (path, mtime, size). Editing .env bumps mtime -> rebuild;
# invalidate_env_cache() is the explicit knob for writers on coarse-mtime filesystems.
_env_cache: Optional[Tuple[Tuple[str, Optional[float], Optional[int]], Dict[str, str]]] = None


def load_env() -> Dict[str, str]:
    """Load ~/.hermes/.env as a dict (memoised; ``get_env_value()`` runs hundreds of times per
    interactive menu render). Each assignment's value is opaque data for boundary discovery."""
    global _env_cache
    env_path = get_env_path()

    try:
        st = env_path.stat()
        cache_key = (str(env_path), st.st_mtime, st.st_size)
    except FileNotFoundError:
        cache_key = (str(env_path), None, None)
    except Exception:
        cache_key = None
    if cache_key is not None and _env_cache is not None and _env_cache[0] == cache_key:
        return dict(_env_cache[1])

    env_vars: Dict[str, str] = {}
    for line in _read_env_lines(env_path) if env_path.exists() else ():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            # Bash-compatible ``export KEY=...`` parses as ``KEY``.
            key, _, value = line.removeprefix('export ').partition('=')
            env_vars[key.strip()] = _parse_env_value(value)
    if cache_key is not None:
        _env_cache = (cache_key, dict(env_vars))
    return env_vars


def invalidate_env_cache() -> None:
    """Clear the load_env() memo so the next call sees a write even on coarse-mtime filesystems."""
    global _env_cache
    _env_cache = None


def _sanitize_env_lines(lines: list) -> list:
    """Normalize .env line endings/whitespace without changing assignment semantics.
    Content after the first ``=`` is opaque value data: a known variable name embedded in a value
    must never be reinterpreted as another assignment, so concatenated lines stay on one line."""
    sanitized: list[str] = []
    for line in lines:
        raw = line.rstrip("\r\n")
        stripped = raw.strip()
        # Blank lines and comments are preserved verbatim.
        sanitized.append((raw if not stripped or stripped.startswith("#") else stripped) + "\n")
    return sanitized


def sanitize_env_file() -> int:
    """Rewrite ~/.hermes/.env with normalized line formatting; returns the number of changed lines."""
    env_path = get_env_path()
    if not env_path.exists():
        return 0
    with open(env_path, encoding="utf-8-sig", errors="replace") as f:
        original_lines = f.readlines()
    sanitized = _sanitize_env_lines(original_lines)
    if sanitized == original_lines:
        return 0
    fixes = abs(len(sanitized) - len(original_lines)) or sum(
        1 for a, b in zip(original_lines, sanitized) if a != b)
    _write_env_lines(env_path, sanitized, preserve_mode=False)
    invalidate_env_cache()
    return fixes


def _read_env_lines(env_path: Path) -> list:
    """Read ``.env`` lines, normalized. Explicit UTF-8 (Windows defaults to cp1252) with BOM
    tolerance (Notepad adds one)."""
    with open(env_path, encoding="utf-8-sig", errors="replace") as f:
        return _sanitize_env_lines(f.readlines())


def _write_env_lines(env_path: Path, lines: list, *, preserve_mode: bool) -> None:
    """Atomically replace ``.env`` (tmp file + fsync + rename).
    ``preserve_mode`` keeps the original file mode (e.g. 0640 for Docker volume mounts) instead of
    letting ``_secure_file`` tighten to 0600; a new file is always secured."""
    original_mode = None
    try:
        original_mode = stat.S_IMODE(env_path.stat().st_mode) if preserve_mode else None
    except OSError:
        pass
    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix=".tmp", prefix=".env_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if original_mode is not None:
        try:
            os.chmod(env_path, original_mode)
        except OSError:
            pass
    else:
        _secure_file(env_path)


def _check_non_ascii_credential(key: str, value: str) -> str:
    """Strip non-ASCII characters from a credential (HTTP header values must be ASCII) and warn.
    Lookalike glyphs typically come from copy-pasting out of a PDF or rich-text editor."""
    if value.isascii():
        return value

    bad_chars = [f"  position {i}: {ch!r} (U+{ord(ch):04X})" for i, ch in enumerate(value) if ord(ch) > 127]
    sanitized = value.encode("ascii", errors="ignore").decode("ascii")

    print(
        f"\n  Warning: {key} contains non-ASCII characters that will break API requests.\n"
        f"  This usually happens when copy-pasting from a PDF, rich-text editor,\n"
        f"  or web page that substitutes lookalike Unicode glyphs for ASCII letters.\n\n"
        + "\n".join(f"  {line}" for line in bad_chars[:5])
        + ("\n  ... and more" if len(bad_chars) > 5 else "")
        + "\n\n  The non-ASCII characters have been stripped automatically.\n"
        "  If authentication fails, re-copy the key from the provider's dashboard.\n",
        file=sys.stderr)
    return sanitized


def _quote_env_value(value: str) -> str:
    """Quote .env values containing characters with special dotenv meaning. Any whitespace
    (including internal runs) is quoted so ``set -a; . file`` word-splitting keeps paths intact."""
    if value == "":
        return value
    if not ("#" in value or '"' in value or "'" in value or any(c.isspace() for c in value)):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _env_line_defines_key(line: str, key: str, *, is_windows: Optional[bool] = None) -> bool:
    """True when a .env line assigns ``key`` — plain, ``export``-prefixed, or ``KEY = value``.
    Must match exactly the shapes ``load_env()`` parses; otherwise a hand-added line is invisible
    to save (duplicate appended) and remove (line survives -> the value resurrects on next load).

    ``load_env()`` accepts the bash-compatible ``export KEY=value`` form (#6659), so the writers must
    recognise the same shape.
    """
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    assigned_key, separator, _value = stripped.partition("=")
    if not separator:
        return False
    # load_env() strips whitespace around the parsed name, so `KEY = value` IS a live assignment. The
    # writers must match the same shape, or a hand-edited spaced line is invisible to save (duplicate
    # appended) and remove (line survives -> value resurrects on next load). #67488.
    return _env_var_policy_name(
        assigned_key.strip(), is_windows=is_windows
    ) == _env_var_policy_name(key, is_windows=is_windows)


def _publish_env_value(key: str, value: Optional[str]) -> None:
    """Publish a just-persisted ``.env`` change to the live process.
    Under a multiplexed gateway a routed profile's write must not land in the SHARED
    ``os.environ`` where every profile sees it; the installed scope mapping is updated instead so
    same-turn reads see the change. All other callers keep the legacy ``os.environ`` publish.

    ``save_env_value`` / ``remove_env_value`` already target the right file (``get_env_path()`` honors the
    profile-home override), but the in-process mirror historically went straight to ``os.environ``. See
    #77490, #88441.
    """
    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active

        scope = current_secret_scope() if is_multiplex_active() else None
    except Exception:
        scope = None
    target = scope if isinstance(scope, dict) else (None if scope is not None else os.environ)
    if target is not None:
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value


def _env_write_blocked(key: str, action: str) -> bool:
    """Shared write-lock check for ``.env`` writers; prints the refusal and returns True when blocked.
    Two distinct locks: ``is_managed()`` (package-manager install) and the managed *scope*
    (administrator-pinned env key — the managed .env wins at load anyway)."""
    if is_managed():
        managed_error(f"{action} {key}")
        return True

    if managed_scope.is_env_managed(key):
        print(
            f"Cannot {action} {key}: it is managed by your administrator ({_managed_source('.env')}) "
            f"and cannot be changed.", file=sys.stderr)
        return True
    return False


def _managed_source(filename: str):
    """``<managed dir>/<filename>`` for refusal messages, or a generic label without a managed dir."""
    managed_dir = managed_scope.get_managed_dir()
    return (managed_dir / filename) if managed_dir else "the managed scope"


def save_env_value(key: str, value: str):
    """Save or update a value in ~/.hermes/.env (also matching ``export KEY=`` lines, so a save
    never appends a second line that a later delete would resurrect)."""
    if _env_write_blocked(key, "set"):
        return
    validate_env_var_name_for_write(key)
    value = value.replace("\n", "").replace("\r", "")
    value = _check_non_ascii_credential(key, value)
    ensure_hermes_home()
    env_path = get_env_path()

    lines = _read_env_lines(env_path) if env_path.exists() else []
    serialized_value = _quote_env_value(value)

    idx = next((i for i, line in enumerate(lines) if _env_line_defines_key(line, key)), None)
    if idx is not None:
        lines[idx] = f"{key}={serialized_value}\n"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={serialized_value}\n")

    _write_env_lines(env_path, lines, preserve_mode=env_path.exists())
    _publish_env_value(key, value)
    invalidate_env_cache()


def custom_endpoint_key_env(identity: str) -> str:
    """Env var name holding a custom endpoint's API key.
    ``identity`` is the endpoint's own id (Desktop endpoint id, or ``host:port`` for CLI setup),
    so two endpoints on one host get separate slots. The fixed ``HERMES_CUSTOM_`` prefix keeps the
    name POSIX-valid when the slug starts with a digit (``save_env_value`` rejects those)."""
    slug = re.sub(r"[^A-Z0-9]+", "_", str(identity or "").upper()).strip("_")
    return f"HERMES_CUSTOM_{slug}_API_KEY" if slug else "HERMES_CUSTOM_API_KEY"


def remove_env_value(key: str) -> bool:
    """Remove a key from ~/.hermes/.env and os.environ; True if it was found and removed."""
    if _env_write_blocked(key, "remove"):
        return False
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    env_path = get_env_path()
    if not env_path.exists():
        _publish_env_value(key, None)
        return False

    lines = _read_env_lines(env_path)
    new_lines = [line for line in lines if not _env_line_defines_key(line, key)]
    found = len(new_lines) < len(lines)
    if found:
        _write_env_lines(env_path, new_lines, preserve_mode=True)
    _publish_env_value(key, None)
    invalidate_env_cache()
    return found


def _write_anthropic_slots(token: str, api_key: str, save_fn=None, *, token_first: bool = True):
    """Write both Anthropic credential slots (one holds the value, the other is cleared)."""
    writer = save_fn or save_env_value
    order = (("ANTHROPIC_TOKEN", token), ("ANTHROPIC_API_KEY", api_key))
    for name, value in order if token_first else reversed(order):
        writer(name, value)


def save_anthropic_oauth_token(value: str, save_fn=None):
    """Persist an Anthropic OAuth/setup token and clear the API-key slot."""
    _write_anthropic_slots(value, "", save_fn)


def use_anthropic_claude_code_credentials(save_fn=None):
    """Use Claude Code's own credential files instead of persisting env tokens."""
    _write_anthropic_slots("", "", save_fn)


def save_anthropic_api_key(value: str, save_fn=None):
    """Persist an Anthropic API key and clear the OAuth/setup-token slot."""
    _write_anthropic_slots("", value, save_fn, token_first=False)


def save_env_value_secure(key: str, value: str) -> Dict[str, Any]:
    """Save via the unified credential lifecycle (also refreshes any config.yaml mirror of the old
    value and lifts a prior env-source suppression)."""
    from hermes_cli.credential_lifecycle import save_provider_env_credential

    # Route through the unified credential lifecycle so a rotation via the secret-capture path also
    # refreshes any config.yaml mirror of the old value and lifts a prior env-source suppression (#62269 fix
    # family).
    save_provider_env_credential(key, value)
    return {"success": True, "stored_as": key, "validated": False}


def reload_env() -> int:
    """Re-read ~/.hermes/.env into os.environ; returns count of vars changed.
    Removes deleted vars only when known to Hermes (OPTIONAL_ENV_VARS and _EXTRA_ENV_KEYS) so
    unrelated environment is never clobbered."""
    env_vars = load_env()
    count = 0
    for key, value in env_vars.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
            count += 1
    for key in (set(OPTIONAL_ENV_VARS) | _EXTRA_ENV_KEYS) - set(env_vars):
        if key in os.environ:
            del os.environ[key]
            count += 1
    return count


def _scoped_environ_get(key: str) -> Optional[str]:
    """Read ``key`` from ``os.environ`` through ``agent.secret_scope.get_secret`` so an active
    profile scope (multiplexed gateway turn) never leaks another profile's raw value. Falls back to
    a plain environ read when the scope module is unavailable; ``UnscopedSecretError`` propagates."""
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret as _get_secret
    except Exception:
        return os.environ.get(key)
    try:
        return _get_secret(key)
    except UnscopedSecretError:
        raise
    except Exception:
        return os.environ.get(key)


def get_env_value(key: str) -> Optional[str]:
    """Get a value from ``os.environ`` (scope-aware) or ``~/.hermes/.env``.

    The ``os.environ`` read routes through ``agent.secret_scope.get_secret`` so that, under an active
    profile scope (multiplexed gateway turn), this is scope-checked rather than leaking another profile's
    raw ``os.environ`` value. ``get_secret`` encodes the whole policy: global vars pass through; scope is
    authoritative under multiplexing (miss -> None, no environ fallthrough); when multiplexing is off it
    behaves exactly like the legacy ``os.environ`` read. Its siblings ``get_env_value_prefer_dotenv`` and
    ``gateway.config._getenv`` already work this way — this was the last scope-blind reader of the trio
    (#67027).
    """
    val = _scoped_environ_get(key)
    return load_env().get(key) if val is None else val


def get_env_value_prefer_dotenv(key: str) -> Optional[str]:
    """Resolve a Hermes-managed credential preferring ``~/.hermes/.env`` over ``os.environ``, so a
    deliberate .env edit beats a stale value inherited from the parent shell."""
    return load_env().get(key) or _scoped_environ_get(key)


# ---- Config display ----

def redact_key(key: str) -> str:
    """Redact an API key for display."""
    from agent.redact import mask_secret
    return mask_secret(key, empty=color("(not set)", Colors.DIM))


# Key names (case-insensitive, exact match) whose VALUE is a credential and must be masked
# before printing any config dict. Exact-match so ``token_count`` / ``secret_santa`` stay visible.
_SECRET_CONFIG_KEYS = frozenset({
    "api_key", "apikey", "key", "token", "access_token", "refresh_token", "id_token",
    "secret", "client_secret", "password", "passwd", "auth", "authorization",
    "private_key", "bearer", "jwt"})


def redact_config_value(value: Any, _depth: int = 0) -> Any:
    """Copy of ``value`` with credential-shaped keys masked. ``print`` bypasses the logging
    redactor and opaque tokens miss the vendor-prefix regexes, so structural masking is required."""
    from agent.redact import mask_secret

    if _depth > 20:  # bound recursion for pathological/cyclic configs
        return value
    if isinstance(value, dict):
        return {
            k: mask_secret(v)
            if isinstance(k, str) and k.lower() in _SECRET_CONFIG_KEYS and isinstance(v, str) and v
            else redact_config_value(v, _depth + 1)
            for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config_value(v, _depth + 1) for v in value]
    return value


def _section(title: str) -> None:
    print()
    print(color(f"◆ {title}", Colors.CYAN, Colors.BOLD))


def _show_managed_banner() -> None:
    """Surface administrator-pinned settings so the user knows why a config.yaml value may not
    be the effective one."""
    managed_keys = managed_scope.managed_config_keys()
    managed_env = managed_scope.load_managed_env()
    if not managed_keys and not managed_env:
        return
    print()
    print(color(
        f"  ⚷ Some settings are managed by your administrator ({managed_scope.get_managed_dir()}) "
        f"and cannot be changed", Colors.YELLOW, Colors.BOLD))
    for label, keys in (("config", managed_keys), ("env", managed_env)):
        if keys:
            print(color(f"    Managed {label} keys: {', '.join(sorted(keys))}", Colors.YELLOW))


_SHOW_CONFIG_API_KEYS = (
    ("OPENROUTER_API_KEY", "OpenRouter"),
    ("VOICE_TOOLS_OPENAI_KEY", "OpenAI (STT/TTS)"),
    ("EXA_API_KEY", "Exa"),
    ("PARALLEL_API_KEY", "Parallel"),
    ("FIRECRAWL_API_KEY", "Firecrawl"),
    ("TAVILY_API_KEY", "Tavily"),
    ("PERPLEXITY_API_KEY", "Perplexity"),
    ("BROWSERBASE_API_KEY", "Browserbase"),
    ("BROWSER_USE_API_KEY", "Browser Use"),
    ("FAL_KEY", "FAL"))


def _show_model_section(config: Dict[str, Any]) -> None:
    _section("Model")
    print(f"  Model:        {redact_config_value(config.get('model', 'not set'))}")
    cfg_max_turns = config.get('agent', {}).get('max_turns', DEFAULT_CONFIG['agent']['max_turns'])
    print(f"  Max turns:    {cfg_max_turns}")
    # Read the .env FILE directly so a stale HERMES_MAX_ITERATIONS ghost is caught even when the
    # gateway bridge already overrode os.environ.
    try:
        env_ghost = load_env().get("HERMES_MAX_ITERATIONS")
    except Exception:
        env_ghost = None
    if env_ghost is not None and str(env_ghost).strip() != str(cfg_max_turns).strip():
        print(color(f"                ⚠ .env has stale HERMES_MAX_ITERATIONS={env_ghost} "
                    f"(run 'hermes doctor --fix' to remove)", Colors.YELLOW))


def _show_display_section(config: Dict[str, Any]) -> None:
    _section("Display")
    display = config.get('display', {})
    try:
        from hermes_cli.personality import active_personality_name
        active_personality = active_personality_name(config) or 'none'
    except Exception:
        active_personality = display.get('personality') or 'none'
    on_off = lambda flag: 'on' if flag else 'off'  # noqa: E731
    print(f"  Personality:  {active_personality}")
    print(f"  Reasoning:    {on_off(display.get('show_reasoning', True))}")
    print(
        f"  Bell:         complete={on_off(display.get('bell_on_complete', False))}, "
        f"prompt={on_off(display.get('bell_on_prompt', False))}")
    ump = display.get('user_message_preview', {})
    ump = ump if isinstance(ump, dict) else {}
    print(f"  User preview: first {ump.get('first_lines', 2)} line(s), last {ump.get('last_lines', 2)} line(s)")


def _show_terminal_section(config: Dict[str, Any]) -> None:
    _section("Terminal")
    terminal = config.get('terminal', {})
    print(f"  Backend:      {terminal.get('backend', 'local')}")
    print(f"  Working dir:  {terminal.get('cwd', '.')}")
    print(f"  Timeout:      {terminal.get('timeout', 60)}s")

    configured = lambda *names: 'configured' if all(get_env_value(n) for n in names) else '(not set)'  # noqa: E731
    default_img = 'nikolaik/python-nodejs:python3.11-nodejs20'
    backend_lines = {
        'docker': lambda: [f"  Docker image: {terminal.get('docker_image', default_img)}"],
        'singularity': lambda: [f"  Image:        {terminal.get('singularity_image', 'docker://' + default_img)}"],
        'modal': lambda: [
            f"  Modal image:  {terminal.get('modal_image', default_img)}",
            f"  Modal token:  {configured('MODAL_TOKEN_ID')}"],
        'daytona': lambda: [
            f"  Daytona image: {terminal.get('daytona_image', default_img)}",
            f"  API key:      {configured('DAYTONA_API_KEY')}"],
        'vercel_sandbox': lambda: [
            f"  Vercel runtime: {terminal.get('vercel_runtime', 'node24')}",
            f"  Vercel auth:    {'configured' if get_env_value('VERCEL_OIDC_TOKEN') or (get_env_value('VERCEL_TOKEN') and get_env_value('VERCEL_PROJECT_ID') and get_env_value('VERCEL_TEAM_ID')) else '(not set)'}",
        ],
        'ssh': lambda: [
            f"  SSH host:     {get_env_value('TERMINAL_SSH_HOST') or '(not set)'}",
            f"  SSH user:     {get_env_value('TERMINAL_SSH_USER') or '(not set)'}"]}
    for line in backend_lines.get(terminal.get('backend'), list)():
        print(line)


def _show_compression_section(config: Dict[str, Any]) -> None:
    _section("Context Compression")
    compression = config.get('compression', {})
    enabled = compression.get('enabled', True)
    print(f"  Enabled:      {'yes' if enabled else 'no'}")
    if not enabled:
        return
    print(f"  Threshold:    {compression.get('threshold', 0.50) * 100:.0f}%")
    tt = compression.get('threshold_tokens')
    try:
        if tt is not None and int(tt) > 0:
            print(f"  Token cap:    {int(tt):,} tokens (takes lower of ratio vs absolute)")
    except (TypeError, ValueError):
        pass
    print(f"  Target ratio: {compression.get('target_ratio', 0.20) * 100:.0f}% of threshold preserved")
    print(f"  Protect last: {compression.get('protect_last_n', 20)} messages")
    print(f"  Protect first: {compression.get('protect_first_n', 3)} non-system head messages")
    aux_comp = config.get('auxiliary', {}).get('compression', {})
    print(f"  Model:        {aux_comp.get('model', '') or '(auto)'}")
    comp_provider = aux_comp.get('provider', 'auto')
    if comp_provider and comp_provider != 'auto':
        print(f"  Provider:     {comp_provider}")


def _show_aux_overrides(config: Dict[str, Any]) -> None:
    aux_tasks = {"Vision": config.get('auxiliary', {}).get('vision', {})}
    overrides = {
        label: (t.get('provider', 'auto'), t.get('model', ''))
        for label, t in aux_tasks.items()
        if t.get('provider', 'auto') != 'auto' or t.get('model', '')}
    if not overrides:
        return
    _section("Auxiliary Models (overrides)")
    for label, (prov, mdl) in overrides.items():
        parts = [f"provider={prov}"] + ([f"model={mdl}"] if mdl else [])
        print(f"  {label:12s}  {', '.join(parts)}")


def _show_skill_settings() -> None:
    try:
        from agent.skill_utils import discover_all_skill_config_vars, resolve_skill_config_values
        skill_vars = discover_all_skill_config_vars()
        if not skill_vars:
            return
        resolved = resolve_skill_config_values(skill_vars)
        _section("Skill Settings")
        for var in skill_vars:
            value = resolved.get(var["key"], "")
            display_val = str(value) if value else color("(not set)", Colors.DIM)
            skill_tag = color(f"[{var.get('skill', '')}]", Colors.DIM)
            print(f"  {var['key']:<20s} {display_val}  {skill_tag}")
    except Exception:
        pass


def show_config():
    """Display current configuration."""
    config = load_config()

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│              ⚕ Hermes Configuration                    │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))
    _show_managed_banner()

    _section("Paths")
    print(f"  Config:       {get_config_path()}")
    print(f"  Secrets:      {get_env_path()}")
    print(f"  Install:      {get_project_root()}")

    _section("API Keys")
    for env_key, name in _SHOW_CONFIG_API_KEYS:
        print(f"  {name:<14} {redact_key(get_env_value(env_key))}")
    from hermes_cli.auth import get_anthropic_key
    print(f"  {'Anthropic':<14} {redact_key(get_anthropic_key())}")

    _show_model_section(config)
    _show_display_section(config)
    _show_terminal_section(config)

    _section("Timezone")
    tz = config.get('timezone', '')
    print(f"  Timezone:     {tz or color('(server-local)', Colors.DIM)}")

    _show_compression_section(config)
    _show_aux_overrides(config)

    _section("Messaging Platforms")
    for label, env_key in (("Telegram", "TELEGRAM_BOT_TOKEN"), ("Discord", "DISCORD_BOT_TOKEN")):
        state = 'configured' if get_env_value(env_key) else color('not configured', Colors.DIM)
        print(f"  {label + ':':<13} {state}")

    _show_skill_settings()

    print()
    print(color("─" * 60, Colors.DIM))
    print(color("  hermes config edit     # Edit config file", Colors.DIM))
    print(color("  hermes config set <key> <value>", Colors.DIM))
    print(color("  hermes setup           # Run setup wizard", Colors.DIM))
    print()


def edit_config():
    """Open config file in user's editor."""
    if is_managed():
        managed_error("edit configuration")
        return
    config_path = get_config_path()
    if not config_path.exists():
        save_config(DEFAULT_CONFIG, strip_defaults=False)
        print(f"Created {config_path}")

    # Windows lands on notepad even without Git Bash/nano; POSIX prefers nano/vim, which headless
    # servers are more likely to have.
    candidates = (['notepad', 'code', 'vim', 'vi', 'nano'] if sys.platform == "win32"
                  else ['nano', 'vim', 'vi', 'code', 'notepad'])
    editor = os.getenv('EDITOR') or os.getenv('VISUAL') or next(
        (cmd for cmd in candidates if shutil.which(cmd)), None)
    if not editor:
        print("No editor found. Config file is at:")
        print(f"  {config_path}")
        return

    print(f"Opening {config_path} in {editor}...")
    subprocess.run([editor, str(config_path)])


# ---- Cron model-drift guard helpers ----

_CRON_DRIFT_AXIS_BY_KEY = {
    "model": "model", "model.default": "model", "model.model": "model", "model.name": "model",
    "model.provider": "provider", "provider": "provider"}


def _cron_model_drift_axis_for_config_key(key: str) -> Optional[str]:
    """Return the cron drift guard axis affected by a config key, if any."""
    return _CRON_DRIFT_AXIS_BY_KEY.get(str(key or "").strip().lower())


def _cron_section(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the ``cron`` mapping of *config* (loading the merged config when None), else None."""
    if config is None:
        try:
            config = load_config()
        except Exception:
            return None
    cron_config = config.get("cron") if isinstance(config, dict) else None
    return cron_config if isinstance(cron_config, dict) else None


def cron_model_drift_guard_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Whether cron must fail closed on unpinned inference drift.
    Only the literal YAML boolean ``false`` disables this spend-safety guard; missing, malformed,
    or non-boolean values stay fail-closed. With *config* omitted the merged config is loaded so
    CLI warnings honor the same user/managed setting as the scheduler."""
    cron_config = _cron_section(config)
    return cron_config is None or cron_config.get("model_drift_guard", True) is not False


_CRON_MODEL_IMPACT_JOB_LIMIT = 50
_CRON_MODEL_IMPACT_ID_LIMIT = 256
_CRON_MODEL_IMPACT_NAME_LIMIT = 120


def _model_assignment_text(value: Any) -> str:
    """Return a trimmed scalar model/provider value, or empty for malformed data."""
    return value.strip() if isinstance(value, str) else ""


def resolve_cron_model_drift_defaults(
    config: Any, *, environ: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """Resolve the global ``(provider, model)`` cron compares against snapshots.
    Mirrors the scheduler's precedence: a truthy configured model wins over ``HERMES_MODEL``; the
    environment is only a fallback. Per-job and cron fleet defaults are handled by the caller
    because they suppress a drift axis rather than changing the global assignment."""
    env = os.environ if environ is None else environ
    provider = ""
    model_config = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_config, dict):
        provider = _model_assignment_text(model_config.get("provider"))
        model_config = model_config.get("default") or model_config.get("model") or model_config.get("name")
    configured_model = _model_assignment_text(model_config)
    return provider, configured_model or _model_assignment_text(env.get("HERMES_MODEL", ""))


def cron_model_drift_axes(
    job: Any, *, current_provider: Any = "", current_model: Any = "", config: Any = None
) -> List[str]:
    """Return the unpinned axes that the fail-closed cron guard would block."""
    if not isinstance(job, dict) or not cron_model_drift_guard_enabled(config):
        return []

    current = {
        "provider": _model_assignment_text(current_provider).lower(),
        "model": _model_assignment_text(current_model).lower()}
    # A cron.model / cron.model_provider fleet default covers its axis: that axis no longer follows
    # the global assignment at fire time, so the guard never engages and a warning would be false.
    fleet = _cron_section(config) or {}
    drifted: List[str] = []
    for axis, fleet_key in (("provider", "model_provider"), ("model", "model")):
        if _model_assignment_text(fleet.get(fleet_key)) or _model_assignment_text(job.get(axis)):
            continue
        snapshot = _model_assignment_text(job.get(f"{axis}_snapshot")).lower()
        if snapshot and current[axis] and snapshot != current[axis]:
            drifted.append(axis)
    return drifted


def _is_control_char(char: str) -> bool:
    return unicodedata.category(char).startswith("C")


def _valid_cron_impact_job_id(value: Any) -> str:
    job_id = value.strip() if isinstance(value, str) else ""
    if len(job_id) > _CRON_MODEL_IMPACT_ID_LIMIT or any(map(_is_control_char, job_id)):
        return ""
    return job_id


def _cron_impact_job_name(value: Any, job_id: str) -> str:
    if isinstance(value, str):
        printable = "".join(char for char in value if not _is_control_char(char))
        name = " ".join(printable.split())[:_CRON_MODEL_IMPACT_NAME_LIMIT].rstrip()
        if name:
            return name
    return f"Job {job_id}"[:_CRON_MODEL_IMPACT_NAME_LIMIT].rstrip()


def _cron_model_impact_result(available: bool, guard_enabled: bool) -> Dict[str, Any]:
    return {
        "available": available,
        "guard_enabled": guard_enabled,
        "affected_count": 0,
        "truncated": False,
        "jobs": []}


def build_cron_model_impact(
    *, current_provider: Any = "", current_model: Any = "", config: Any = None, jobs: Any = None
) -> Dict[str, Any]:
    """Build a bounded, profile-local summary of jobs blocked by model drift.
    Job-store inspection is best effort: the model assignment has already succeeded when Desktop
    requests this, so an unreadable store is reported as unavailable rather than failing."""
    guard_enabled = cron_model_drift_guard_enabled(config)
    if jobs is None:
        try:
            from cron.jobs import load_jobs

            jobs = load_jobs()
        except Exception:
            return _cron_model_impact_result(False, guard_enabled)
    if not isinstance(jobs, list):
        return _cron_model_impact_result(False, guard_enabled)

    result = _cron_model_impact_result(True, guard_enabled)
    if not guard_enabled:
        return result

    from cron.jobs import is_job_runnable

    seen_ids: Set[str] = set()
    for job in jobs:
        if not isinstance(job, dict) or not is_job_runnable(job) or job.get("no_agent"):
            continue
        job_id = _valid_cron_impact_job_id(job.get("id"))
        if not job_id or job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        axes = cron_model_drift_axes(
            job, current_provider=current_provider, current_model=current_model, config=config)
        if not axes:
            continue
        result["affected_count"] += 1
        if len(result["jobs"]) < _CRON_MODEL_IMPACT_JOB_LIMIT:
            result["jobs"].append({
                "id": job_id,
                "name": _cron_impact_job_name(job.get("name"), job_id),
                "drifted_axes": axes})

    result["truncated"] = result["affected_count"] > len(result["jobs"])
    return result


def warn_unpinned_cron_jobs_after_model_config_change(
    key: str, value: Any, config: Optional[Dict[str, Any]] = None) -> None:
    """Warn when a global model/provider change will trip cron's drift guard."""
    axis = _cron_model_drift_axis_for_config_key(key)
    if axis is None:
        return

    new_value = _model_assignment_text(value)
    if not new_value:
        return
    impact = build_cron_model_impact(
        current_provider=new_value if axis == "provider" else "",
        current_model=new_value if axis == "model" else "", config=config, jobs=None)
    affected = impact["affected_count"]
    if affected <= 0:
        return

    noun, verb = ("job", "has") if affected == 1 else ("jobs", "have")
    print(
        f"⚠️  {affected} enabled unpinned cron {noun} {verb} stored "
        f"{axis}_snapshot values that differ from the new global {axis}. "
        "They will fail closed on their next run instead of silently using the changed "
        "model/provider. Inspect with `hermes cron list`, then pin the intended values with "
        "`hermes cron edit <job_id> --provider <provider> --model <model>`.")


def _default_value_for_key(dotted_key: str):
    """Return the leaf value declared for *dotted_key* in ``DEFAULT_CONFIG`` (None for dicts/misses)."""
    node = cfg_get(DEFAULT_CONFIG, *_split_key_path(dotted_key))
    return None if isinstance(node, dict) else node


# Top-level keys that accept arbitrary user-supplied child keys (schema declares the dict, the
# user populates it): any path below is accepted without deep checking.
_OPEN_DICT_TOP_LEVEL_KEYS = frozenset({
    "providers", "credential_pool_strategies", "mcp_servers", "hooks", "quick_commands",
    "personalities", "command_allowlist", "model_catalog", "channel_prompts", "server_actions",
    "secrets", "goals", "loops"})

# Top-level keys whose sub-keys are partially schema-defined (e.g. a PlatformConfig dataclass) but
# where users may add fields DEFAULT_CONFIG doesn't enumerate: validate the FIRST segment only.
_SCHEMA_DEFINED_DICT_KEYS = frozenset({
    # Platform configs — PlatformConfig dataclass + dynamic extras
    "discord", "telegram", "slack", "whatsapp", "signal", "mattermost",
    "matrix", "feishu", "wecom", "weixin", "bluebubbles", "qqbot", "yuanbao",
    "email", "sms", "dingtalk",
    # MCP server template / dynamic auth dicts
    "sessions", "checkpoints",
    # Plugin enable/disable lists + index_url override; absent from DEFAULT_CONFIG.
    "plugins"})

# Top-level keys that can be ANY user-supplied name.
_DYNAMIC_TOP_LEVEL_KEYS = frozenset({
    "custom_providers",  # list-shaped, but indexed by position
})

# Containers whose immediate child IS a user-supplied platform name (``platforms.<name>.<field>``),
# both top-level and under ``gateway``; anything below the name is accepted (open ``extra``).
_PLATFORM_CONTAINER_KEYS = frozenset({"platforms"})


# Top-level keys whose sub-keys are accepted without deep checking.
_OPEN_SUBKEY_TOP_LEVEL_KEYS = _OPEN_DICT_TOP_LEVEL_KEYS | _DYNAMIC_TOP_LEVEL_KEYS | _SCHEMA_DEFINED_DICT_KEYS


def _known_top_level_keys() -> set[str]:
    """Return the union of known top-level config keys for validation."""
    return set(DEFAULT_CONFIG) | _OPEN_SUBKEY_TOP_LEVEL_KEYS


def _suggest_closest_key(key: str, candidates: set[str], cutoff: float = 0.6) -> Optional[str]:
    """Closest candidate key name for a typo'd ``key``, or None."""
    return next(iter(difflib.get_close_matches(key, sorted(candidates), n=1, cutoff=cutoff)), None)


def _validate_config_key(key: str) -> tuple[bool, Optional[str]]:
    """Validate a dotted config-key path against the known schema -> ``(is_known, suggestion)``.

    Headline case from #34067: ``gateway.discord.gateway_restart_notification`` was silently written, even
    though ``gateway`` only has 4 known sub-keys (``strict``, ``media_delivery_allow_dirs``,
    ``trust_recent_files``, ``trust_recent_files_seconds``). The correct path is
    ``discord.gateway_restart_notification`` (platform configs live at the top level, not under a
    ``platforms`` namespace).
    """
    if not key:
        return False, None

    segments = _split_key_path(key)
    top = segments[0]

    # A leading underscore on the FIRST segment marks an intentionally non-schema internal key
    # (test harnesses/tooling); only the first segment is exempt so ``agent._max_turns`` is caught.
    if top.startswith("_") or top in _PLATFORM_CONTAINER_KEYS:
        return True, None

    known = _known_top_level_keys()
    if top not in known:
        suggestion = _suggest_closest_key(top, known)
        if suggestion is None:
            return False, None
        rest = ".".join(segments[1:])
        return False, f"{suggestion}.{rest}" if rest else suggestion

    if top in _OPEN_SUBKEY_TOP_LEVEL_KEYS:
        return True, None

    # Walk DEFAULT_CONFIG: a nested ``platforms`` container or a scalar leaf hit before the path is
    # consumed both accept (the latter matches set_config_value's leaf->dict replacement); an
    # unknown sub-key fails with a same-level "did you mean" suggestion.
    node: Any = DEFAULT_CONFIG.get(top)
    consumed = [top]
    for seg in segments[1:]:
        if seg in _PLATFORM_CONTAINER_KEYS or not isinstance(node, dict):
            return True, None
        if seg not in node:
            sibling = _suggest_closest_key(seg, set(node.keys()))
            return False, ".".join(consumed + [sibling]) if sibling is not None else None
        consumed.append(seg)
        node = node[seg]
    return True, None


def _looks_structured_value(value: str) -> bool:
    """True when *value* plausibly encodes a YAML/JSON list or mapping. Deliberately conservative:
    a bare leading ``-`` is not a trigger (``-5``, ``--flag`` must stay strings)."""
    stripped = value.lstrip()
    if stripped[:1] in ('[', '{'):
        return True
    if '\n' not in value:
        return False
    for line in value.splitlines():
        item = line.strip()
        if item == '-' or item.startswith('- '):
            return True
        # ``key: value`` / ``key:`` mapping-entry shape (no whitespace in the key).
        head, sep, _rest = item.partition(': ')
        if sep and head and ' ' not in head and not head.startswith('#'):
            return True
        if item.endswith(':') and ' ' not in item[:-1] and item[:-1]:
            return True
    return False


def _coerce_int(value: str):
    """int(value) for a clean integer literal (signs/whitespace/underscores OK), else None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: str):
    """``float(value)`` only when the conversion preserves its decimal value; NaN/inf rejected.
    Decimal-looking identifiers more precise than a binary float must stay strings."""
    try:
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")) or Decimal(value) != Decimal(str(f)):
            return None
    except (TypeError, ValueError, InvalidOperation):
        return None
    return f


_SCALAR_WORDS = {
    'true': True, 'yes': True, 'on': True,
    'false': False, 'no': False, 'off': False,
    # YAML null. Many DEFAULT_CONFIG leaves are "null/absent = off"; without this,
    # ``config set X null`` stored the truthy string "null" and the feature could never be cleared.
    'null': None, 'none': None, '~': None}


def _coerce_config_set_value(key: str, value: str) -> Any:
    """Auto-coerce a ``hermes config set`` string to bool/None/int/float/list/dict.
    String-typed settings (per ``DEFAULT_CONFIG``) are preserved verbatim so enum members such as
    ``approvals.mode="off"`` never become booleans. List/mapping literals are parsed so
    isinstance-gated readers see real structures; the trigger is conservative."""
    if isinstance(_default_value_for_key(key), str):
        return value
    stripped = value.strip()
    lower = stripped.lower()
    if lower in _SCALAR_WORDS:
        return _SCALAR_WORDS[lower]
    for coerce in (_coerce_int, _coerce_float):
        coerced = coerce(stripped)
        if coerced is not None:
            return coerced
    if not _looks_structured_value(value):
        return value
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        print(
            f"Warning: value for '{key}' looks like a list/mapping but is "
            f"not valid YAML/JSON; storing as string. Most isinstance-gated "
            f"readers will ignore a string here.", file=sys.stderr)
        return value
    if isinstance(parsed, (list, dict)):
        return parsed
    print(
        f"Warning: value for '{key}' looks like a list/mapping but "
        f"parsed as {type(parsed).__name__}; storing as string.", file=sys.stderr)
    return value


def _redirect_platform_display_key(key: str) -> tuple[str, Optional[str]]:
    """Canonicalize ``platforms.<name>.<display_setting>`` -> ``display.platforms.<name>.<setting>``.
    The gateway resolves per-platform display settings (streaming, show_reasoning, ...) from
    ``display.platforms``; the top-level ``platforms.<name>`` block holds only connection config.
    Only known display settings (``OVERRIDEABLE_KEYS``) are redirected. Returns ``(key, note)``;
    the gateway import is guarded so the CLI works where the gateway package is unavailable.

    Before #71047 a write such as ``hermes config set platforms.telegram.streaming false`` landed on a key
    the gateway never reads: ``config get`` echoed the new value back while the runtime kept the old
    ``display.platforms`` one — a silent no-op that looks like a duplicated key to the user.
    """
    segs = _split_key_path(key)
    if len(segs) != 3 or segs[0] != "platforms":
        return key, None
    try:
        from gateway.display_config import OVERRIDEABLE_KEYS as _display_keys
    except Exception:
        return key, None
    if segs[2] not in _display_keys:
        return key, None
    canonical = f"display.platforms.{segs[1]}.{segs[2]}"
    return canonical, f"  (note: per-platform display setting — saved as {canonical})"


def _exit_if_key_managed(key: str, action: str) -> None:
    """A key pinned by the managed layer cannot be set/unset (the next load would reinstate it):
    hard-reject and name the source. Distinct from ``is_managed()``; env-shaped keys route to the
    .env writers, which carry their own guard."""
    if managed_scope.is_key_managed(key):
        print(
            f"Cannot {action} '{key}': it is managed by your administrator ({_managed_source('config.yaml')}) "
            f"and cannot be changed. Contact your administrator to modify it.", file=sys.stderr)
        sys.exit(1)


def _guard_section_overwrite(key: str, value: Any, user_config: Dict[str, Any], force: bool) -> str:
    """Refuse (or with ``force`` allow) a single-segment key overwriting a mapping with a scalar.
    Bare ``model`` is a documented shorthand — redirected to ``model.default`` so siblings survive.
    Returns the (possibly redirected) key."""
    existing = user_config.get(key)
    if "." in key or not isinstance(existing, dict):
        return key
    if key == "model":
        if force:
            print(
                f"⚠ Replacing entire 'model' section with a scalar "
                f"(discarding {len(existing)} existing sub-key(s))")
            return key
        print(
            f"✓ Redirecting bare 'model' to 'model.default' "
            f"(preserving {len(existing)} existing model sub-key(s))")
        return "model.default"
    if force:
        return key
    sub = [k for k in existing if isinstance(k, str)]
    err = [
        f"✗ Cannot set '{key}' to a scalar — '{key}' is a "
        f"configuration section with {len(sub)} sub-key(s)."]
    if sub:
        err.append(f"  Sub-keys: {', '.join(sub[:8])}")
        if len(sub) > 8:
            err.append(f"  ... and {len(sub) - 8} more")
    err += [
        "  Use a dotted path to set a specific leaf key:",
        f"    hermes config set {key}.<sub-key> <value>",
        "  Or use --force to replace the entire section:",
        f"    hermes config set --force {key} {value!r}"]
    print("\n".join(err), file=sys.stderr)
    sys.exit(1)


def _touch_skin_file(key: str, value: Any) -> None:
    """``display.skin`` set means "apply NOW": bump the skin file's mtime so the gateway watcher's
    (name, mtime) signature moves even when the name is unchanged. Best-effort."""
    if key == "display.skin" and isinstance(value, str) and value:
        try:
            skin_file = get_hermes_home() / "skins" / f"{value}.yaml"
            if skin_file.exists():
                skin_file.touch()
        except Exception:
            pass


def _exit_invalid(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def _write_user_config(config_path: Path, user_config: Dict[str, Any]) -> None:
    """Write only the user's raw config back (never the merged defaults)."""
    ensure_hermes_home()
    atomic_yaml_write(config_path, user_config, sort_keys=False)


def _print_unknown_key_notice(key: str, suggestion: Optional[str]) -> None:
    print(color(
        f"⚠ '{key}' is not a recognized config key — it was saved anyway, "
        "but Hermes may not read it.", Colors.YELLOW))
    if suggestion:
        print(color(f"  Did you mean: {suggestion}", Colors.YELLOW))
    print(color(
        "  (Custom top-level keys are supported and bridged to the "
        "environment for skills/external tools. Use --force to skip "
        "this notice.)", Colors.DIM))


def set_config_value(key: str, value: str, force: bool = False):
    """Set a configuration value at a dotted ``key``; ``value`` is auto-coerced to bool/int/float.
    ``force`` skips the unknown-key warning AND authorizes replacing a mapping section with a
    scalar. Without it, scalar writes over mappings are refused and bare ``model`` is redirected
    to ``model.default``."""
    if is_managed():
        managed_error("set configuration values")
        return
    # Empty segments (``"agent."``) would write config["agent"][""] into a live schema section.
    if key != key.strip() or not key.strip():
        _exit_invalid(f"✗ Invalid config key: {key!r} (empty or surrounding whitespace).")
    if "" in _split_key_path(key):
        _exit_invalid(
            f"✗ Invalid config key: {key!r} — contains an empty path segment "
            "(leading, trailing, or doubled '.').")
    _exit_if_key_managed(key, "set")
    if _is_env_config_key(key):
        # Unified lifecycle: also rotates any config.yaml mirror of the old value.
        from hermes_cli.credential_lifecycle import save_provider_env_credential

        # Unified lifecycle: also rotates any config.yaml mirror of the old value so a stale
        # higher-precedence copy can't win (#62269).
        save_provider_env_credential(key.upper(), value)
        print(f"✓ Set {key} in {get_env_path()}")
        return

    # Canonicalize per-platform display keys BEFORE validation/coercion so both see the path the
    # runtime reads. Unknown keys are still written (top-level scalars are bridged into os.environ
    # for skills/external apps) but get a post-write "did you mean" hint.
    key, _redirect_note = _redirect_platform_display_key(key)
    if _redirect_note:
        # Unknown-key notice (#34067): the key is still written (arbitrary keys are supported — top-level
        # scalars are bridged into os.environ for skills and external apps), but a plausible-but-wrong
        # dotted path like ``gateway.discord.gateway_restart_notification`` previously reported bare success
        # and left the user debugging behavior that never changed. Warn after the write so the user gets
        # immediate feedback plus a "did you mean" hint, without blocking legitimate unknown keys.
        print(_redirect_note)
    is_known, suggestion = _validate_config_key(key)

    # Read the RAW user config (not merged) so defaults are never dumped back; fail-closed.
    config_path = get_config_path()
    user_config = require_readable_config_before_write(config_path)
    value = _coerce_config_set_value(key, value)
    # A scalar ``model`` shorthand must become a dict before writing sub-keys, or _set_nested
    # replaces it with an empty dict and the model id is lost.
    _model_val = user_config.get("model")
    if key.strip().lower().startswith("model.") and isinstance(_model_val, str) and _model_val:
        user_config["model"] = {"default": _model_val}
    key = _guard_section_overwrite(key, value, user_config, force)
    try:
        _set_nested(user_config, key, value)
    except ValueError as e:
        _exit_invalid(f"✗ {e}")
    # api_base -> base_url alias at set-time too (mirrors _normalize_root_model_keys).
    if key.strip().lower() in ("model.api_base", "api_base"):
        # Normalize the api_base → base_url alias at set-time too (issue #8919), so a fresh `hermes config
        # set model.api_base ...` lands on the canonical key the runtime resolver actually reads, instead of
        # being silently ignored.
        user_config = _normalize_root_model_keys(user_config)
        key = "model.base_url"
        print("  (note: 'api_base' is an alias — saved as model.base_url)")
    _write_user_config(config_path, user_config)

    # Keep .env in sync: terminal_tool reads TERMINAL_ENV etc. directly from env vars.
    env_var = terminal_config_env_var_for_key(key)
    if env_var and key != "terminal.cwd":
        save_env_value(env_var, _terminal_env_value(value))

    _touch_skin_file(key, value)

    # Mask the echoed value when the (possibly nested) key is credential-shaped, e.g.
    # ``model.api_key`` (lowercase, so it misses the .env routing above).
    _display_value = value
    if key.rsplit(".", 1)[-1].lower() in _SECRET_CONFIG_KEYS and isinstance(value, str) and value:
        from agent.redact import mask_secret
        _display_value = mask_secret(value)
    print(f"✓ Set {key} = {_display_value} in {config_path}")
    warn_unpinned_cron_jobs_after_model_config_change(key, value, user_config)

    # Post-write unknown-key notice (#34067): value IS saved, but tell the user the runtime may never read
    # it and suggest the likely-intended path.
    if not is_known and not force:
        _print_unknown_key_notice(key, suggestion)


def get_config_value(key: str, *, as_json: bool = False):
    """Print a resolved configuration value."""
    if _is_env_config_key(key):
        env_value = get_env_value(key.upper())
        value = _MISSING if env_value is None else env_value
    else:
        # Mirror set_config_value: read the canonical display.platforms path.
        # See #71047.
        key, _ = _redirect_platform_display_key(key)
        value = _get_nested(load_config(), key)

    if value is _MISSING:
        _exit_invalid(f"Config key not set: {key}")

    print(_format_config_get_value(value, as_json=as_json))


def unset_config_value(key: str):
    """Remove a user-set configuration or .env value."""
    if is_managed():
        managed_error("unset configuration values")
        return
    _exit_if_key_managed(key, "unset")

    if _is_env_config_key(key):
        # Unified lifecycle: also prunes env-seeded credential_pool entries and model-cache rows so
        # the provider is fully removed instead of left resurrectable.
        # See #51071.
        from hermes_cli.credential_lifecycle import remove_provider_env_credential

        if not remove_provider_env_credential(key.upper()).get("found"):
            _exit_invalid(f"Config key not set: {key}")
        print(f"✓ Unset {key} from {get_env_path()}")
        return

    config_path = get_config_path()
    user_config = require_readable_config_before_write(config_path)

    key, _redirect_note = _redirect_platform_display_key(key)
    if _redirect_note:
        # Mirror set_config_value's display.platforms canonicalization (#71047).
        print(_redirect_note.replace("saved as", "resolved as"))
    removed = _unset_nested(user_config, key)

    env_var = terminal_config_env_var_for_key(key)
    if env_var and key != "terminal.cwd":
        removed = remove_env_value(env_var) or removed

    if not removed:
        _exit_invalid(f"Config key not set: {key}")

    _write_user_config(config_path, user_config)
    print(f"✓ Unset {key} from {config_path}")


# ---- Command handler ----

def _usage_exit(usage: str, examples: List[str], extra: Optional[List[str]] = None) -> None:
    print(usage)
    print()
    print("Examples:")
    for line in examples:
        print(f"  {line}")
    for line in extra or ():
        print(line)
    sys.exit(1)


def _run_write_command(fn, *args) -> None:
    """Run a config writer, surfacing the fail-closed write guard's RuntimeError as a clean CLI
    error instead of a traceback."""
    try:
        fn(*args)
    except RuntimeError as exc:
        _exit_invalid(f"✗ {exc}")


_USAGE_GET = ("Usage: hermes config get <key> [--json]", [
    "hermes config get model", "hermes config get terminal.backend",
    "hermes config get skills.config --json"], None)
_USAGE_SET = ("Usage: hermes config set [--force] <key> <value>", [
    "hermes config set model anthropic/claude-sonnet-4", "hermes config set terminal.backend docker",
    "hermes config set OPENROUTER_API_KEY sk-or-..."], [
    "", "  --force: skip the unknown-key notice for unrecognized keys,",
    "           and allow a scalar to replace a whole mapping section"])
_USAGE_UNSET = ("Usage: hermes config unset <key>", [
    "hermes config unset model", "hermes config unset terminal.backend",
    "hermes config unset OPENROUTER_API_KEY"], None)


def _cmd_config_get(args):
    key = getattr(args, 'key', None)
    if not key:
        _usage_exit(*_USAGE_GET)
    get_config_value(key, as_json=getattr(args, 'json', False))


def _cmd_config_set(args):
    key = getattr(args, 'key', None)
    value = getattr(args, 'value', None)
    if not key or value is None:
        _usage_exit(*_USAGE_SET)
    _run_write_command(set_config_value, key, value, bool(getattr(args, 'force', False)))


def _cmd_config_unset(args):
    key = getattr(args, 'key', None)
    if not key:
        _usage_exit(*_USAGE_UNSET)
    _run_write_command(unset_config_value, key)


def _tools_suffix(info: Dict[str, Any], fmt: str) -> str:
    tools = info.get("tools", [])
    return fmt.format(", ".join(tools[:2])) if tools else ""


def _print_banner(text: str) -> None:
    print()
    print(color(text, Colors.CYAN, Colors.BOLD))
    print()


def _cmd_config_migrate(args):
    _print_banner("🔄 Checking configuration for updates...")

    missing_env = get_missing_env_vars(required_only=False)
    missing_config = get_missing_config_fields()
    current_ver, latest_ver = check_config_version(raise_on_parse_error=True)

    if not missing_env and not missing_config and current_ver >= latest_ver:
        print(color("✓ Configuration is up to date!", Colors.GREEN))
        print()
        return

    if current_ver < latest_ver:
        print(f"  Config version: {current_ver} → {latest_ver}")

    if missing_config:
        print(f"\n  {len(missing_config)} new config option(s) will be added with defaults")

    required_missing = [v for v in missing_env if v.get("is_required")]
    optional_missing = [v for v in missing_env if not v.get("is_required") and not v.get("advanced")]
    for heading, group, suffix in (
        ("⚠️  {} required API key(s) missing:", required_missing, ""),
        ("ℹ️  {} optional API key(s) not configured:", optional_missing, " (enables: {})")):
        if group:
            print(f"\n  {heading.format(len(group))}")
            for var in group:
                print(f"     • {var['name']}{_tools_suffix(var, suffix) if suffix else ''}")

    print()
    results = migrate_config(interactive=True, quiet=False)
    print()
    if results["env_added"] or results["config_added"]:
        print(color("✓ Configuration updated!", Colors.GREEN))
    if results["warnings"]:
        print()
        for warning in results["warnings"]:
            print(color(f"  ⚠️  {warning}", Colors.YELLOW))
    print()


def _cmd_config_check(args):
    """Non-interactive report of what's missing."""
    _print_banner("📋 Configuration Status")

    current_ver, latest_ver = check_config_version(raise_on_parse_error=True)
    if current_ver >= latest_ver:
        print(f"  Config version: {current_ver} ✓")
    else:
        print(color(f"  Config version: {current_ver} → {latest_ver} (update available)", Colors.YELLOW))

    groups = (
        ("Required", REQUIRED_ENV_VARS, lambda n, i: color(f"    ✗ {n} (missing)", Colors.RED)),
        ("Optional", OPTIONAL_ENV_VARS,
         lambda n, i: color(f"    ○ {n}{_tools_suffix(i, ' → {}')}", Colors.DIM)))
    for title, table, missing_line in groups:
        print()
        print(color(f"  {title}:", Colors.BOLD))
        for var_name, info in table.items():
            print(f"    ✓ {var_name}" if get_env_value(var_name) else missing_line(var_name, info))

    missing_config = get_missing_config_fields()
    if missing_config:
        print()
        print(color(f"  {len(missing_config)} new config option(s) available", Colors.YELLOW))
        print("    Run 'hermes config migrate' to add them")

    print()


_CONFIG_SUBCOMMANDS = {
    None: lambda args: show_config(),
    "show": lambda args: show_config(),
    "edit": lambda args: edit_config(),
    "get": _cmd_config_get,
    "set": _cmd_config_set,
    "unset": _cmd_config_unset,
    "path": lambda args: print(get_config_path()),
    "env-path": lambda args: print(get_env_path()),
    "migrate": _cmd_config_migrate,
    "check": _cmd_config_check}

_CONFIG_USAGE = """Available commands:
  hermes config           Show current configuration
  hermes config edit      Open config in editor
  hermes config get <key>          Print a resolved config value
  hermes config set <key> <value>   Set a config value
  hermes config unset <key>        Remove a config value
  hermes config check     Check for missing/outdated config
  hermes config migrate   Update config with new options
  hermes config path      Show config file path
  hermes config env-path  Show .env file path"""


def config_command(args):
    """Handle config subcommands."""
    subcmd = getattr(args, 'config_command', None)
    handler = _CONFIG_SUBCOMMANDS.get(subcmd)
    if handler is not None:
        handler(args)
        return
    print(f"Unknown config command: {subcmd}")
    print()
    print(_CONFIG_USAGE)
    sys.exit(1)


# ---- OPTIONAL_ENV_VARS injection from provider profiles and platform plugins (once, at import) ----

def _inject_profile_env_vars() -> None:
    """Expose env_vars of every ``auth_type="api_key"`` provider in providers/ via OPTIONAL_ENV_VARS
    without editing this file."""
    try:
        from providers import list_providers
        for _pp in list_providers():
            if _pp.auth_type != "api_key":
                continue
            for _var in _pp.env_vars:
                if _var in OPTIONAL_ENV_VARS:
                    continue
                _is_key = not _var.endswith(("_BASE_URL", "_URL"))
                _label = _pp.display_name or _pp.name
                OPTIONAL_ENV_VARS[_var] = {
                    "description": f"{_label} {'API key' if _is_key else 'base URL override'}",
                    "prompt": f"{_label} {'API key' if _is_key else 'base URL (leave empty for default)'}",
                    "url": _pp.signup_url or None,
                    "password": _is_key,
                    "category": "provider",
                    "advanced": True}
    except Exception:
        pass


_inject_profile_env_vars()


def _platform_plugin_manifests():
    """Yield ``(dir_name, manifest_dict)`` for every bundled ``plugins/platforms/*/plugin.y(a)ml``."""
    platforms_dir = get_project_root() / "plugins" / "platforms"
    if not platforms_dir.is_dir():
        return
    for child in platforms_dir.iterdir():
        manifest_path = next(
            (p for p in (child / "plugin.yaml", child / "plugin.yml") if child.is_dir() and p.exists()), None)
        if manifest_path is None:
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = fast_safe_load(f) or {}
        except Exception:
            continue
        yield child.name, manifest


def _inject_platform_plugin_env_vars() -> None:
    """Populate OPTIONAL_ENV_VARS from bundled platform plugin manifests so Teams / IRC / Google
    Chat etc. are configurable in ``hermes config`` UI without the core knowing they exist.

    ``requires_env`` / ``optional_env`` entries are a bare name or a dict with ``name`` plus
    optional ``description``/``url``/``password``/``prompt``/``category``. Failures are swallowed
    so a malformed plugin.yaml can't break CLI import.
    """
    try:
        for dir_name, manifest in _platform_plugin_manifests():
            label = manifest.get("label") or manifest.get("name") or dir_name
            for entry in [*(manifest.get("requires_env") or []), *(manifest.get("optional_env") or [])]:
                meta = {"name": entry} if isinstance(entry, str) else entry if isinstance(entry, dict) else {}
                name = meta.get("name")
                if not name or name in OPTIONAL_ENV_VARS:
                    continue  # hardcoded entry wins (back-compat)
                # *TOKEN / *SECRET / *KEY / *PASSWORD / *JSON are password fields unless overridden.
                is_secret = bool(meta.get("password") or meta.get("secret"))
                if not is_secret and not meta.get("password") is False:
                    is_secret = name.upper().endswith(("_TOKEN", "_SECRET", "_KEY", "_PASSWORD", "_JSON"))
                OPTIONAL_ENV_VARS[name] = {
                    "description": meta.get("description") or f"{label} configuration",
                    "prompt": meta.get("prompt") or name,
                    "url": meta.get("url") or None,
                    "password": is_secret,
                    "category": meta.get("category") or "messaging"}
    except Exception:
        pass


_inject_platform_plugin_env_vars()


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

def _install_method_project_root(project_root: Optional[Path] = None) -> Path:
    """Resolve the directory that holds the *running code* (the install tree).

    This is the parent of ``hermes_cli/`` — i.e. the git checkout for source
    installs, ``/opt/hermes`` inside the published image. It is a property of
    the running interpreter, NOT of ``$HERMES_HOME``, which is why a
    code-scoped stamp here is immune to two installs sharing one data
    directory.
    """
    if project_root is not None:
        return project_root
    return Path(__file__).parent.parent.resolve()

def stamp_install_method(method: str, project_root: Optional[Path] = None) -> None:
    """Write the install method next to the running code (code-scoped stamp).

    The stamp lives in the install tree (``<install tree>/.install_method``),
    not in ``$HERMES_HOME``, so that two installs sharing one data directory
    do not overwrite each other's marker. See ``detect_install_method`` for
    the full rationale.

    Best-effort: if the install tree is read-only (e.g. the immutable
    ``/opt/hermes`` in the published image, which instead bakes the stamp at
    build time) the write silently no-ops and detection falls back to its
    other signals.
    """
    root = _install_method_project_root(project_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".install_method").write_text(method + "\n", encoding="utf-8")
    except OSError:
        pass


_PLUGIN_COMPAT_LAZY = {
    'normalize_route_base_url': ('hermes_cli.route_identity', 'normalize_route_base_url'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----

"""Tests for ``install_cua_driver`` upgrade semantics.

The cua-driver upstream installer always pulls the latest release tag, so
re-running it is the canonical upgrade path. ``install_cua_driver(upgrade=True)``
must:

* Be cross-platform — run on macOS, Windows, and Linux. Only genuinely
  unsupported platforms no-op silently on upgrade so ``hermes update`` can
  call it unconditionally without warning those users.
* Choose the right installer per OS: ``install.sh`` via ``curl | bash`` on
  macOS/Linux, ``install.ps1`` via PowerShell ``irm | iex`` on Windows.
* Re-run the installer even when the binary is already on PATH (this is the
  fix for the "we only pulled cua-driver once on enable" complaint).
* Preserve original ``upgrade=False`` behaviour for the toolset-enable flow:
  skip if installed, install otherwise, warn on non-macOS.

The pre-install arch probe that used to live alongside this function was
deleted (see top-of-file comment in tools_config.py) — the upstream
installer has CUA_DRIVER_RS_BAKED_VERSION baked in by CD and errors
cleanly on missing-arch assets, and the upgrade path uses
``cua_driver_update_check()`` (which shells `cua-driver check-update
--json` against the already-installed binary).
  skip if installed, install otherwise, warn on unsupported platforms.
* Pre-check architecture compatibility before downloading to avoid raw 404
  errors when the upstream release lacks an asset for this OS+arch.
"""

from __future__ import annotations

from unittest.mock import patch


class TestInstallCuaDriverUpgrade:
    def test_upgrade_on_unsupported_platform_is_silent_noop(self):
        from hermes_cli import tools_config

        with patch.object(tools_config, "_print_warning") as warn, \
             patch("platform.system", return_value="FreeBSD"):
            assert tools_config.install_cua_driver(upgrade=True) is False
            warn.assert_not_called()

    def test_non_upgrade_on_unsupported_platform_warns(self):
        from hermes_cli import tools_config

        with patch.object(tools_config, "_print_warning") as warn, \
             patch("platform.system", return_value="FreeBSD"):
            assert tools_config.install_cua_driver(upgrade=False) is False
            warn.assert_called()

    def test_upgrade_on_macos_with_binary_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in {"cua-driver", "curl"} else None), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner, \
             patch("subprocess.run"):
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()
            kwargs = runner.call_args.kwargs
            assert kwargs.get("verbose") is False

    def test_upgrade_on_macos_without_binary_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()

    def test_non_upgrade_on_macos_with_binary_skips_install(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in {"cua-driver", "curl"} else None), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner, \
             patch("subprocess.run"):
            assert tools_config.install_cua_driver(upgrade=False) is True
            runner.assert_not_called()

    def test_non_upgrade_on_macos_without_binary_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=False) is True
            runner.assert_called_once()


class TestArchProbeRemoval:
    """Regression tests for the deletion of `_check_cua_driver_asset_for_arch`.

    The old probe queried ``/releases/latest`` on trycua/cua and inspected
    asset names. That was wrong in two ways:

    1. cua-driver-rs releases are marked **prerelease** on every cut, so
       ``/releases/latest`` returns the Python ``cua-agent`` / ``cua-computer``
       package instead — a release with zero binary assets. The probe then
       reported "no asset for $arch" on Linux x86_64, Windows, macOS Intel,
       Linux arm64 — every non-Apple-Silicon host.
    2. Even with the right endpoint, it duplicated tag-resolution the upstream
       installer already does correctly via ``CUA_DRIVER_RS_BAKED_VERSION``
       (auto-baked by CD on every release).

    The fix: stop probing. Trust the upstream installer for fresh installs
    (it has the baked version + correct API fallback) and the
    ``cua-driver check-update --json`` MCP-binary native command for the
    upgrade path.
    """

    def test_probe_function_is_gone(self):
class TestCheckCuaDriverAssetForArch:
    def test_arm64_macos_always_returns_true(self):
        from hermes_cli import tools_config
        assert not hasattr(tools_config, "_check_cua_driver_asset_for_arch")
        assert not hasattr(tools_config, "_latest_cua_driver_rs_release")

    def test_fresh_install_does_not_call_github_api(self):
        """Pre-install no longer probes the GitHub API — the upstream
        ``install.sh`` resolves the tag from its baked CUA_DRIVER_RS_BAKED_VERSION
        line. install.sh errors cleanly when the arch has no asset, so the
        probe was duplicate gatekeeping.
        """
        # Apple Silicon assets are always published — short-circuits without
        # a network probe.
        with patch("platform.system", return_value="Darwin"), \
             patch("platform.machine", return_value="arm64"):
            assert tools_config._check_cua_driver_asset_for_arch() is True

    def test_x86_64_with_asset_returns_true(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch("urllib.request.urlopen") as urlopen, \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=False) is True
            runner.assert_called_once()
            urlopen.assert_not_called()

    def test_upgrade_with_binary_does_not_call_github_api_directly(self):
        """The upgrade path no longer hits GitHub from Python — it delegates
        to the upstream ``install.sh`` (which has the baked release tag and
        the proper API fallback). When cua-driver is already installed,
        ``cua_driver_update_check()`` (added in a separate change) further
        short-circuits the network re-install via the binary's native
        ``check-update --json`` verb.
        """
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in ("cua-driver", "curl") else None), \
             patch("urllib.request.urlopen") as urlopen, \
             patch("subprocess.run"), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()
            # Probe deleted — no direct GitHub API call from Python.
            urlopen.assert_not_called()
            runner.assert_not_called()

        # Without binary — returns False
        with patch("platform.system", return_value="Darwin"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch("platform.machine", return_value="x86_64"), \
             patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(tools_config, "_print_warning"), \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner:
            assert tools_config.install_cua_driver(upgrade=True) is False
            runner.assert_not_called()


class TestInstallCuaDriverWindows:
    """install_cua_driver dispatch on Windows hosts."""

    def test_fresh_install_runs_installer(self):
        from hermes_cli import tools_config

        # PowerShell present, cua-driver not yet installed.
        with patch("platform.system", return_value="Windows"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: r"C:\\Windows\\powershell.exe"
                                                 if n == "powershell" else None), \
             patch.object(tools_config, "_check_cua_driver_asset_for_arch",
                          return_value=True), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=False) is True
            runner.assert_called_once()

    def test_fresh_install_without_powershell_fails(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Windows"), \
             patch.object(tools_config.shutil, "which", lambda n: None), \
             patch.object(tools_config, "_print_warning") as warn, \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_run_cua_driver_installer") as runner:
            assert tools_config.install_cua_driver(upgrade=False) is False
            runner.assert_not_called()
            # The warning should name the missing fetch tool (powershell).
            assert "powershell" in warn.call_args[0][0].lower()

    def test_upgrade_with_binary_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Windows"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: r"C:\\bin\\" + n
                                                 if n in {"cua-driver", "powershell"} else None), \
             patch.object(tools_config, "_check_cua_driver_asset_for_arch",
                          return_value=True), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner, \
             patch("subprocess.run"):
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()
            assert runner.call_args.kwargs.get("verbose") is False

    def test_installer_uses_powershell_irm_command(self):
        """_run_cua_driver_installer must shell out to PowerShell irm|iex."""
        from hermes_cli import tools_config

        completed = MagicMock(returncode=0)
        with patch("platform.system", return_value="Windows"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: r"C:\\bin\\" + n
                                                 if n == "cua-driver" else None), \
             patch("subprocess.run", return_value=completed) as run, \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_print_success"), \
             patch.object(tools_config, "_print_warning"):
            assert tools_config._run_cua_driver_installer() is True
            cmd = run.call_args[0][0]
            # Argument list (shell=False), not a string.
            assert isinstance(cmd, list)
            assert cmd[0] == "powershell"
            assert run.call_args.kwargs.get("shell") is False
            joined = " ".join(cmd)
            assert "install.ps1" in joined
            assert "iex" in joined


class TestInstallCuaDriverLinux:
    """install_cua_driver dispatch on Linux hosts (alpha)."""

    def test_fresh_install_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Linux"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/bin/curl" if n == "curl" else None), \
             patch.object(tools_config, "_check_cua_driver_asset_for_arch",
                          return_value=True), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner:
            assert tools_config.install_cua_driver(upgrade=False) is True
            runner.assert_called_once()

    def test_upgrade_with_binary_runs_installer(self):
        from hermes_cli import tools_config

        with patch("platform.system", return_value="Linux"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n in {"cua-driver", "curl"} else None), \
             patch.object(tools_config, "_check_cua_driver_asset_for_arch",
                          return_value=True), \
             patch.object(tools_config, "_run_cua_driver_installer",
                          return_value=True) as runner, \
             patch("subprocess.run"):
            assert tools_config.install_cua_driver(upgrade=True) is True
            runner.assert_called_once()

    def test_installer_uses_curl_bash_command(self):
        """_run_cua_driver_installer must shell out to curl | bash install.sh."""
        from hermes_cli import tools_config

        completed = MagicMock(returncode=0)
        with patch("platform.system", return_value="Linux"), \
             patch.object(tools_config.shutil, "which",
                          side_effect=lambda n: "/usr/local/bin/" + n
                                                 if n == "cua-driver" else None), \
             patch("subprocess.run", return_value=completed) as run, \
             patch.object(tools_config, "_print_info"), \
             patch.object(tools_config, "_print_success"), \
             patch.object(tools_config, "_print_warning"):
            assert tools_config._run_cua_driver_installer() is True
            cmd = run.call_args[0][0]
            assert isinstance(cmd, str)  # shell string on POSIX
            assert run.call_args.kwargs.get("shell") is True
            assert "install.sh" in cmd
            assert "curl" in cmd


class TestCheckCuaDriverAssetCrossPlatform:
    """_check_cua_driver_asset_for_arch recognizes Windows/Linux asset names."""

    @staticmethod
    def _mock_release(asset_names):
        release = {"tag_name": "cua-driver-v0.5.0",
                   "assets": [{"name": n} for n in asset_names]}
        resp = MagicMock()
        resp.read.return_value = json.dumps(release).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_windows_amd64_with_asset_returns_true(self):
        from hermes_cli import tools_config

        resp = self._mock_release([
            "cua-driver-0.5.0-windows-amd64.zip",
            "cua-driver-0.5.0-darwin-arm64.tar.gz",
        ])
        with patch("platform.system", return_value="Windows"), \
             patch("platform.machine", return_value="AMD64"), \
             patch("urllib.request.urlopen", return_value=resp):
            assert tools_config._check_cua_driver_asset_for_arch() is True

    def test_windows_arm64_without_asset_returns_false(self):
        from hermes_cli import tools_config

        resp = self._mock_release([
            "cua-driver-0.5.0-windows-amd64.zip",
        ])
        with patch("platform.system", return_value="Windows"), \
             patch("platform.machine", return_value="ARM64"), \
             patch("urllib.request.urlopen", return_value=resp), \
             patch.object(tools_config, "_print_warning") as warn, \
             patch.object(tools_config, "_print_info"):
            assert tools_config._check_cua_driver_asset_for_arch() is False
            warn.assert_called_once()
            assert "arm64" in warn.call_args[0][0].lower()

    def test_linux_x86_64_with_asset_returns_true(self):
        from hermes_cli import tools_config

        resp = self._mock_release([
            "cua-driver-0.5.0-linux-x86_64.tar.gz",
        ])
        with patch("platform.system", return_value="Linux"), \
             patch("platform.machine", return_value="x86_64"), \
             patch("urllib.request.urlopen", return_value=resp):
            assert tools_config._check_cua_driver_asset_for_arch() is True

    def test_linux_aarch64_with_asset_returns_true(self):
        from hermes_cli import tools_config

        resp = self._mock_release([
            "cua-driver-0.5.0-linux-aarch64.tar.gz",
        ])
        with patch("platform.system", return_value="Linux"), \
             patch("platform.machine", return_value="aarch64"), \
             patch("urllib.request.urlopen", return_value=resp):
            assert tools_config._check_cua_driver_asset_for_arch() is True

    def test_linux_aarch64_without_asset_returns_false(self):
        from hermes_cli import tools_config

        resp = self._mock_release([
            "cua-driver-0.5.0-linux-x86_64.tar.gz",
        ])
        with patch("platform.system", return_value="Linux"), \
             patch("platform.machine", return_value="aarch64"), \
             patch("urllib.request.urlopen", return_value=resp), \
             patch.object(tools_config, "_print_warning") as warn, \
             patch.object(tools_config, "_print_info"):
            assert tools_config._check_cua_driver_asset_for_arch() is False
            warn.assert_called_once()

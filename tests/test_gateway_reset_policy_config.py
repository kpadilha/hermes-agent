"""Regression tests for config.yaml gateway reset-policy overrides."""
from pathlib import Path
from unittest.mock import MagicMock, patch



def _load_with_yaml_dict(yaml_dict: dict):
    """Patch filesystem so load_gateway_config() sees *yaml_dict* as config.yaml."""
    from gateway.config import Platform, load_gateway_config

    fake_home = Path("/tmp/fake_hermes_home_reset_policy")

    def fake_exists(self):
        return str(self).endswith("config.yaml")

    with patch("gateway.config.get_hermes_home", return_value=fake_home), \
         patch.object(Path, "exists", fake_exists), \
         patch("builtins.open", create=True) as mock_file:
        mock_file.return_value.__enter__ = lambda s: s
        mock_file.return_value.__exit__ = MagicMock(return_value=False)
        with patch("yaml.safe_load", return_value=yaml_dict):
            return load_gateway_config(), Platform



def test_top_level_reset_by_type_is_honored_for_threads():
    cfg, Platform = _load_with_yaml_dict({
        "reset_by_type": {
            "thread": {"mode": "idle", "idle_minutes": 4320, "notify": True}
        }
    })

    policy = cfg.get_reset_policy(platform=Platform.DISCORD, session_type="thread")
    assert policy.mode == "idle"
    assert policy.idle_minutes == 4320
    assert policy.notify is True



def test_top_level_reset_by_platform_takes_precedence_over_type():
    cfg, Platform = _load_with_yaml_dict({
        "reset_by_type": {
            "thread": {"mode": "idle", "idle_minutes": 4320}
        },
        "reset_by_platform": {
            "discord": {"mode": "none", "idle_minutes": 1}
        },
    })

    policy = cfg.get_reset_policy(platform=Platform.DISCORD, session_type="thread")
    assert policy.mode == "none"
    assert policy.idle_minutes == 1

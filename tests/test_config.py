import pytest

from bot.config import Config, ConfigError

BASE = {"DERIV_API_TOKEN": "t", "DERIV_APP_ID": "1"}


def test_defaults_to_demo_and_watch():
    cfg = Config.from_env(dict(BASE))
    assert cfg.account_type == "demo"
    assert cfg.strategy == "watch"
    assert not cfg.is_real


def test_token_required():
    with pytest.raises(ConfigError, match="DERIV_API_TOKEN"):
        Config.from_env({"DERIV_APP_ID": "1"})


def test_real_needs_explicit_confirmation():
    with pytest.raises(ConfigError, match="real-money"):
        Config.from_env({**BASE, "DERIV_ACCOUNT_TYPE": "real"})
    cfg = Config.from_env({**BASE, "DERIV_ACCOUNT_TYPE": "real",
                           "ALLOW_REAL_MONEY": "YES_I_UNDERSTAND"})
    assert cfg.is_real


@pytest.mark.parametrize("env", [
    {"STAKE": "3", "MAX_STAKE": "2"},
    {"MAX_STAKE": "10", "MAX_DAILY_LOSS": "5"},
    {"STAKE": "0"},
    {"STAKE": "abc"},
])
def test_rejects_unsafe_limits(env):
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, **env})


def test_token_not_in_repr():
    assert "zzzsecret" not in repr(Config.from_env({**BASE, "DERIV_API_TOKEN": "zzzsecret"}))

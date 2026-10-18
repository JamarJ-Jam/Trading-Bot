import pytest

from bot.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(
        api_token="secret-token",
        app_id="12345",
        stake=1.0,
        max_stake=2.0,
        max_daily_loss=3.0,
        max_trades_per_day=10,
        max_open_trades=1,
        max_consecutive_losses=2,
        pause_minutes=30,
        log_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
        kill_switch_file=tmp_path / "STOP",
    )

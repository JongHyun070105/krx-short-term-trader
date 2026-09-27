import pytest

from krx_trader.config import Settings


def test_settings_defaults_are_shadow_and_capital_bounded():
    settings = Settings.from_env({}, env_file=None)
    assert settings.trading_mode == "shadow"
    assert settings.live_trading_enabled is False
    assert settings.max_live_capital_krw == 100_000
    assert settings.credential_status()["KIS_APP_SECRET"] is False


def test_effective_price_respects_order_cap_and_buy_cost_reserve():
    settings = Settings.from_env({}, env_file=None)
    assert settings.max_price_krw == 50_000
    assert settings.effective_max_price_krw == 19_967


def test_paper_mode_is_rejected_as_out_of_scope():
    with pytest.raises(ValueError, match="paper is out of scope"):
        Settings.from_env({"TRADING_MODE": "paper"}, env_file=None)


def test_hard_cap_cannot_be_increased():
    with pytest.raises(ValueError, match="100000"):
        Settings.from_env({"MAX_LIVE_CAPITAL_KRW": "100001"}, env_file=None)


def test_account_digits_are_separate():
    with pytest.raises(ValueError, match="first 8 account digits"):
        Settings.from_env({"KIS_ACCOUNT_NO": "1234567801"}, env_file=None)

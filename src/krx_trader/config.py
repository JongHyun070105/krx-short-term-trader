from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _bool(value: str, key: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account_no: str = ""
    kis_account_product_code: str = ""
    kis_hts_id: str = ""
    trading_mode: str = "shadow"
    live_trading_enabled: bool = False
    max_live_capital_krw: int = 100_000
    max_order_notional_krw: int = 20_000
    max_concurrent_positions: int = 2
    risk_per_trade_pct: float = 0.25
    max_daily_loss_pct: float = 0.75
    max_daily_trades: int = 4
    min_price_krw: int = 1_000
    max_price_krw: int = 50_000
    broker_fee_rate: float = 0.00015
    sell_tax_rate: float = 0.0020
    slippage_bps: float = 15.0
    regime_trend_lookback: int = 20
    regime_volatility_lookback: int = 20
    regime_high_vol_threshold: float = 0.025

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        env_file: Path | None = Path(".env"),
    ) -> Settings:
        merged = _read_env_file(env_file) if env_file is not None and env is None else {}
        merged.update(os.environ if env is None else env)

        def text(name: str, default: str = "") -> str:
            return merged.get(name, default).strip()

        def integer(name: str, default: int) -> int:
            try:
                return int(text(name, str(default)))
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc

        def number(name: str, default: float) -> float:
            try:
                return float(text(name, str(default)))
            except ValueError as exc:
                raise ValueError(f"{name} must be numeric") from exc

        settings = cls(
            kis_app_key=text("KIS_APP_KEY"),
            kis_app_secret=text("KIS_APP_SECRET"),
            kis_account_no=text("KIS_ACCOUNT_NO"),
            kis_account_product_code=text("KIS_ACCOUNT_PRODUCT_CODE"),
            kis_hts_id=text("KIS_HTS_ID"),
            trading_mode=text("TRADING_MODE", "shadow").lower(),
            live_trading_enabled=_bool(text("LIVE_TRADING_ENABLED", "false"), "LIVE_TRADING_ENABLED"),
            max_live_capital_krw=integer("MAX_LIVE_CAPITAL_KRW", 100_000),
            max_order_notional_krw=integer("MAX_ORDER_NOTIONAL_KRW", 20_000),
            max_concurrent_positions=integer("MAX_CONCURRENT_POSITIONS", 2),
            risk_per_trade_pct=number("RISK_PER_TRADE_PCT", 0.25),
            max_daily_loss_pct=number("MAX_DAILY_LOSS_PCT", 0.75),
            max_daily_trades=integer("MAX_DAILY_TRADES", 4),
            min_price_krw=integer("MIN_PRICE_KRW", 1_000),
            max_price_krw=integer("MAX_PRICE_KRW", 50_000),
            broker_fee_rate=number("BROKER_FEE_RATE", 0.00015),
            sell_tax_rate=number("SELL_TAX_RATE", 0.0020),
            slippage_bps=number("SLIPPAGE_BPS", 15.0),
            regime_trend_lookback=integer("REGIME_TREND_LOOKBACK", 20),
            regime_volatility_lookback=integer("REGIME_VOLATILITY_LOOKBACK", 20),
            regime_high_vol_threshold=number("REGIME_HIGH_VOL_THRESHOLD", 0.025),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.trading_mode not in {"shadow", "live"}:
            raise ValueError("TRADING_MODE must be shadow or live; paper is out of scope")
        if not 0 < self.max_live_capital_krw <= 100_000:
            raise ValueError("MAX_LIVE_CAPITAL_KRW must be between 1 and 100000")
        if not 0 < self.max_order_notional_krw <= min(self.max_live_capital_krw, 50_000):
            raise ValueError("MAX_ORDER_NOTIONAL_KRW must not exceed 50000 or the live capital cap")
        if self.max_concurrent_positions < 1 or self.max_daily_trades < 1:
            raise ValueError("position and daily trade limits must be positive")
        if min(self.regime_trend_lookback, self.regime_volatility_lookback) < 2 or self.regime_high_vol_threshold <= 0:
            raise ValueError("invalid regime lookbacks or high-volatility threshold")
        if not 0 < self.risk_per_trade_pct <= 1 or not 0 < self.max_daily_loss_pct <= 100:
            raise ValueError("risk percentages are outside safe configured ranges")
        if self.min_price_krw <= 0 or self.max_price_krw < self.min_price_krw:
            raise ValueError("invalid price range")
        if self.broker_fee_rate < 0 or self.sell_tax_rate < 0 or self.slippage_bps < 0:
            raise ValueError("cost assumptions cannot be negative")
        if self.broker_fee_rate + self.sell_tax_rate >= 1 or self.slippage_bps >= 10_000:
            raise ValueError("cost assumptions exceed valid execution bounds")
        if self.kis_account_no and (len(self.kis_account_no) != 8 or not self.kis_account_no.isdigit()):
            raise ValueError("KIS_ACCOUNT_NO must contain the first 8 account digits only")
        if self.kis_account_product_code and (len(self.kis_account_product_code) != 2 or not self.kis_account_product_code.isdigit()):
            raise ValueError("KIS_ACCOUNT_PRODUCT_CODE must contain 2 digits")

    def credential_status(self) -> dict[str, bool]:
        return {
            "KIS_APP_KEY": bool(self.kis_app_key),
            "KIS_APP_SECRET": bool(self.kis_app_secret),
            "KIS_ACCOUNT_NO": bool(self.kis_account_no),
            "KIS_ACCOUNT_PRODUCT_CODE": bool(self.kis_account_product_code),
            "KIS_HTS_ID": bool(self.kis_hts_id),
        }

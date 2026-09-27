from __future__ import annotations

from dataclasses import dataclass


class LiveBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LiveGates:
    trading_mode: str
    live_trading_enabled: bool
    confirm_live: bool
    capital_cap_krw: int
    open_exposure_krw: int
    pending_buy_notional_krw: int = 0
    reserved_notional_krw: int = 0
    order_cap_krw: int = 20_000


def authorize_live_order(gates: LiveGates, order_notional_krw: int) -> None:
    if gates.trading_mode != "live":
        raise LiveBlocked("TRADING_MODE must be live")
    if not gates.live_trading_enabled:
        raise LiveBlocked("LIVE_TRADING_ENABLED must be true")
    if not gates.confirm_live:
        raise LiveBlocked("explicit --confirm-live acknowledgement is required")
    if not 0 < gates.capital_cap_krw <= 100_000:
        raise LiveBlocked("capital cap exceeds the hard 100000 KRW limit")
    exposure = gates.open_exposure_krw + gates.pending_buy_notional_krw + gates.reserved_notional_krw
    if min(gates.open_exposure_krw, gates.pending_buy_notional_krw, gates.reserved_notional_krw) < 0:
        raise LiveBlocked("negative exposure state")
    if exposure + order_notional_krw > gates.capital_cap_krw:
        raise LiveBlocked("order would exceed the capital cap")
    if not 0 < gates.order_cap_krw <= min(gates.capital_cap_krw, 50_000):
        raise LiveBlocked("per-order cap is invalid")
    if order_notional_krw > gates.order_cap_krw:
        raise LiveBlocked("order exceeds the configured per-order cap")


class LiveBrokerAdapter:
    """Fail-closed boundary; no KIS order endpoint is enabled in this release."""

    def submit(self, gates: LiveGates, order_notional_krw: int) -> None:
        authorize_live_order(gates, order_notional_krw)
        raise LiveBlocked("KIS order submission is unavailable until separately implemented and reviewed")

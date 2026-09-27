from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class RiskDay:
    session_date: date
    realized_pnl_krw: float = 0.0
    trades: int = 0
    kill_reasons: tuple[str, ...] = ()


class DailyRiskState:
    """Persistent, local-only daily loss and kill-switch state."""

    def __init__(self, path: Path = Path("runtime/risk_state.json")) -> None:
        self.path = path

    def load(self, now: datetime | None = None) -> RiskDay:
        today = (now or datetime.now(KST)).astimezone(KST).date()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return RiskDay(today)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("persistent risk state cannot be read; new entries must remain blocked") from exc
        if not isinstance(raw, dict) or "session_date" not in raw:
            raise RuntimeError("persistent risk state is malformed; new entries must remain blocked")
        try:
            if raw.get("session_date") != today.isoformat():
                return RiskDay(today)
            return RiskDay(
                today,
                float(raw.get("realized_pnl_krw", 0)),
                int(raw.get("trades", 0)),
                tuple(str(item) for item in raw.get("kill_reasons", [])),
            )
        except (ValueError, TypeError) as exc:
            raise RuntimeError("persistent risk state is malformed; new entries must remain blocked") from exc

    def save(self, state: RiskDay) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(
            {
                "session_date": state.session_date.isoformat(),
                "realized_pnl_krw": state.realized_pnl_krw,
                "trades": state.trades,
                "kill_reasons": list(state.kill_reasons),
            },
            sort_keys=True,
        )
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def record_trade(self, realized_pnl_krw: float, now: datetime | None = None) -> RiskDay:
        state = self.load(now)
        updated = RiskDay(state.session_date, state.realized_pnl_krw + realized_pnl_krw, state.trades + 1, state.kill_reasons)
        self.save(updated)
        return updated

    def activate(self, reason: str, now: datetime | None = None) -> RiskDay:
        state = self.load(now)
        reasons = tuple(dict.fromkeys((*state.kill_reasons, reason)))
        updated = RiskDay(state.session_date, state.realized_pnl_krw, state.trades, reasons)
        self.save(updated)
        return updated

    def loss_limit_breached(
        self, capital_krw: float, max_daily_loss_pct: float, *, open_risk_krw: float = 0.0,
        now: datetime | None = None,
    ) -> bool:
        if capital_krw <= 0 or max_daily_loss_pct <= 0 or open_risk_krw < 0:
            raise ValueError("invalid daily loss inputs")
        state = self.load(now)
        loss_limit = capital_krw * max_daily_loss_pct / 100
        return state.realized_pnl_krw - open_risk_krw <= -loss_limit

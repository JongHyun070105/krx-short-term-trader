from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar


def load_bars_csv(path: Path) -> list[Bar]:
    """Load canonical OHLCV CSV with ISO timestamps; timestamps must carry an offset."""
    bars: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("CSV must include timestamp,open,high,low,close,volume")
        for row in reader:
            bars.append(
                Bar(
                    time=datetime.fromisoformat(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )
            )
    require_healthy_bars(bars)
    return bars

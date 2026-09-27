from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from krx_trader.data.quality import require_healthy_bars
from krx_trader.models import Bar

KST = ZoneInfo("Asia/Seoul")


class ParquetBarCache:
    """Crash-safe, per-symbol Parquet partitions with a provenance sidecar."""

    def __init__(self, root: Path = Path("data")) -> None:
        self.root = root

    def partition_path(self, kind: str, symbol: str, interval: str, session_date: date | None = None) -> Path:
        if kind not in {"daily", "minute", "indexes"}:
            raise ValueError("kind must be daily, minute, or indexes")
        if not symbol or any(part in symbol for part in ("/", "\\", "..")):
            raise ValueError("invalid symbol or index identifier")
        if kind == "minute":
            if session_date is None:
                raise ValueError("minute cache partitions require a session date")
            return self.root / kind / symbol / f"{session_date.isoformat()}.parquet"
        return self.root / kind / f"{symbol}-{interval}.parquet"

    def contains(self, kind: str, symbol: str, interval: str, session_date: date | None = None) -> bool:
        return self.partition_path(kind, symbol, interval, session_date).is_file()

    def save(
        self,
        bars: list[Bar],
        *,
        kind: str,
        symbol: str,
        interval: str,
        market: str,
        source: str,
        requested_at: datetime | None = None,
        session_date: date | None = None,
        requested_start: date | None = None,
        requested_end: date | None = None,
    ) -> tuple[Path, dict[str, object]]:
        if not bars:
            raise ValueError("refusing to cache an empty OHLCV dataset")
        ordered = sorted(bars, key=lambda bar: bar.time)
        timestamps = [bar.time for bar in ordered]
        if len(timestamps) != len(set(timestamps)) or any(item.tzinfo is None for item in timestamps):
            raise ValueError("cache requires unique, timezone-aware bar timestamps")
        require_healthy_bars(ordered)
        path = self.partition_path(kind, symbol, interval, session_date)
        rows = [
            {
                "timestamp": bar.time.astimezone(KST).isoformat(),
                "open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
                "close": float(bar.close), "volume": int(bar.volume),
            }
            for bar in ordered
        ]
        table = pa.Table.from_pylist(rows)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            pq.write_table(table, temp_path, compression="zstd")
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        metadata: dict[str, object] = {
            "source": source,
            "requested_at": (requested_at or datetime.now(KST)).astimezone(KST).isoformat(),
            "market": market,
            "symbol": symbol,
            "interval": interval,
            "first_timestamp": ordered[0].time.astimezone(KST).isoformat(),
            "last_timestamp": ordered[-1].time.astimezone(KST).isoformat(),
            "rows": len(ordered),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        if requested_start is not None:
            metadata["requested_start"] = requested_start.isoformat()
        if requested_end is not None:
            metadata["requested_end"] = requested_end.isoformat()
        sidecar = path.with_suffix(".metadata.json")
        with tempfile.NamedTemporaryFile(prefix=f".{sidecar.name}.", suffix=".tmp", dir=path.parent, delete=False) as tmp:
            metadata_temp = Path(tmp.name)
        try:
            metadata_temp.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(metadata_temp, sidecar)
        finally:
            metadata_temp.unlink(missing_ok=True)
        return path, metadata

    def load(
        self,
        kind: str,
        symbol: str,
        interval: str,
        session_date: date | None = None,
    ) -> list[Bar]:
        path = self.partition_path(kind, symbol, interval, session_date)
        metadata_path = path.with_suffix(".metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if metadata.get("sha256") != actual_hash:
            raise ValueError("Parquet cache hash does not match its provenance sidecar")
        rows = pq.read_table(path).to_pylist()
        bars = [
            Bar(
                time=datetime.fromisoformat(row["timestamp"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
            )
            for row in rows
        ]
        require_healthy_bars(bars)
        if len(bars) != metadata.get("rows"):
            raise ValueError("Parquet row count does not match its provenance sidecar")
        return bars

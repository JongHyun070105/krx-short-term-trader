from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from krx_trader.universe.models import StockMaster

KST = ZoneInfo("Asia/Seoul")
MASTER_URLS = {
    "KOSPI": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}
_TAIL_LAYOUTS = {
    # KIS's official examples use 228 KOSPI and 222 KOSDAQ suffix characters
    # while iterating lines (including '\n'). splitlines() removes that newline.
    "KOSPI": {
        "width": 227, "security_group": (0, 2), "etp": (22, 23), "spac": (29, 30),
        "reference_price": (41, 50), "halted": (60, 61), "management": (62, 63),
        "market_warning": (63, 65), "warning_alert": (65, 66), "preferred": (158, 159),
        "market_cap": (212, 221),
    },
    "KOSDAQ": {
        "width": 221, "security_group": (0, 2), "etp": (18, 19), "spac": (24, 25),
        "reference_price": (36, 45), "halted": (55, 56), "management": (57, 58),
        "market_warning": (58, 60), "warning_alert": (60, 61), "preferred": (153, 154),
        "market_cap": (206, 215),
    },
}


def _integer(value: str) -> int | None:
    digits = value.strip()
    if not digits or not digits.isdigit():
        return None
    number = int(digits)
    return number if number > 0 else None


def _active_flag(value: str) -> bool:
    # KIS uses Y/1 for binary flags and "2" for its ETP-type marker.
    return value.strip().upper() in {"Y", "1", "2"}


def _active_etp(value: str) -> bool:
    # ETP product codes are 0 for none and 1..5 for ETF/ETN product types.
    return value.strip().upper() in {"Y", "1", "2", "3", "4", "5"}


def parse_master_archive(payload: bytes, market: str) -> list[StockMaster]:
    if market not in MASTER_URLS:
        raise ValueError("market must be KOSPI or KOSDAQ")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".mst")]
            if len(members) != 1:
                raise ValueError("KIS stock-master archive layout was unexpected")
            records = archive.read(members[0]).decode("cp949").splitlines()
    except (OSError, zipfile.BadZipFile, UnicodeError) as exc:
        raise ValueError(f"invalid KIS {market} stock-master archive ({type(exc).__name__})") from None

    layout = _TAIL_LAYOUTS[market]
    tail_width = int(layout["width"])
    stocks: list[StockMaster] = []
    for record in records:
        if len(record) <= tail_width:
            continue
        prefix, tail = record[:-tail_width], record[-tail_width:]
        symbol = prefix[:9].strip()
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        name = prefix[21:].strip()
        if not name:
            continue
        values = {
            key: tail[start:end].strip()
            for key, field_range in layout.items()
            if key != "width"
            for start, end in [field_range]
        }
        etp = _active_etp(values["etp"])
        spac = _active_flag(values["spac"])
        preferred = _active_flag(values["preferred"])
        warning_raw = values["market_warning"]
        warning_alert = _active_flag(values["warning_alert"])
        warning_status = warning_raw if warning_raw.strip("0") else None
        if warning_alert and warning_status is None:
            warning_status = "WARNING_ALERT"
        security_group = values["security_group"].upper()
        kind = (
            "ETF_ETN" if etp else "SPAC" if spac else "PREFERRED" if preferred else
            "COMMON" if security_group == "ST" else "OTHER"
        )
        stocks.append(
            StockMaster(
                symbol=symbol,
                name=name,
                market=market,
                instrument_type=kind,
                listing_status="CURRENT_MASTER",
                reference_price=_integer(values["reference_price"]),
                market_cap_raw=_integer(values["market_cap"]),
                halted=_active_flag(values["halted"]),
                management=_active_flag(values["management"]),
                warning_status=warning_status,
                is_preferred=preferred,
                is_etp=etp,
                is_spac=spac,
            )
        )
    if not stocks:
        raise ValueError(f"KIS {market} stock master contained no six-digit listings")
    return stocks


def _atomic_write_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        pq.write_table(table, temp_path, compression="zstd")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def refresh_stock_master(path: Path = Path("data/universe/stocks.parquet")) -> dict[str, object]:
    rows: list[StockMaster] = []
    source_hashes: dict[str, str] = {}
    for market, url in MASTER_URLS.items():
        request = urllib.request.Request(url, headers={"User-Agent": "krx-short-term-trader/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
        except OSError as exc:
            raise RuntimeError(f"KIS {market} stock master download failed ({type(exc).__name__})") from None
        source_hashes[market] = hashlib.sha256(payload).hexdigest()
        rows.extend(parse_master_archive(payload, market))

    rows.sort(key=lambda item: (item.market, item.symbol))
    _atomic_write_parquet(path, [
        {
            "symbol": row.symbol,
            "name": row.name,
            "market": row.market,
            "instrument_type": row.instrument_type,
            "listing_status": row.listing_status,
            "reference_price": row.reference_price,
            "market_cap_raw": row.market_cap_raw,
            "halted": row.halted,
            "management": row.management,
            "warning_status": row.warning_status,
            "is_preferred": row.is_preferred,
            "is_etp": row.is_etp,
            "is_spac": row.is_spac,
        }
        for row in rows
    ])
    metadata = {
        "source": "Korea Investment official KRX stock-master archives",
        "source_urls": MASTER_URLS,
        "source_sha256": source_hashes,
        "requested_at": datetime.now(KST).isoformat(),
        "markets": sorted({row.market for row in rows}),
        "symbols": len(rows),
        "common_stocks": sum(row.instrument_type == "COMMON" for row in rows),
        "parquet_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    metadata_path = path.with_suffix(".metadata.json")
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return metadata


def load_stock_master(path: Path = Path("data/universe/stocks.parquet")) -> list[StockMaster]:
    table = pq.read_table(path)
    return [StockMaster(**row) for row in table.to_pylist()]

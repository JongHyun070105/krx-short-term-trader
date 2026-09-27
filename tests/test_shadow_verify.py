from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from krx_trader.config import Settings
from krx_trader.execution.realtime_shadow import RealtimeShadowRunner
from krx_trader.execution.shadow_verify import verify_shadow_run
from krx_trader.market.regime import Regime
from krx_trader.models import Bar

KST = ZoneInfo("Asia/Seoul")


def test_shadow_verifier_replays_signal_from_saved_warmup_and_index_inputs(tmp_path) -> None:
    start = datetime(2026, 9, 28, 9, tzinfo=KST)
    observed = datetime(2026, 9, 28, 14, 16, tzinfo=KST)
    runner = RealtimeShadowRunner(
        Settings(),
        object(),  # type: ignore[arg-type]
        run_id="verify-shadow",
        scheduled_start=start,
        stop_at=datetime(2026, 9, 28, 13, tzinfo=KST),
        root=tmp_path,
        index_bars={"kospi": [], "kosdaq": []},
        clock=lambda: observed,
    )

    completed = [
        Bar(start + timedelta(minutes=15 * (index + 1)), 1_000.0, 1_005.0, 995.0, 1_000.0, 100)
        for index in range(20)
    ]
    completed.append(Bar(start + timedelta(minutes=15 * 21), 1_000.0, 1_012.0, 995.0, 1_010.0, 1_000))
    minute_groups: list[list[Bar]] = []
    for interval_bar in completed:
        bucket_start = interval_bar.time - timedelta(minutes=15)
        group = []
        for offset in range(15):
            open_price = interval_bar.open if offset == 0 else interval_bar.close
            close_price = interval_bar.close if offset == 14 else open_price
            high = interval_bar.high if offset == 7 else max(open_price, close_price)
            low = interval_bar.low if offset == 8 else min(open_price, close_price)
            group.append(Bar(
                bucket_start + timedelta(minutes=offset), open_price, high, low, close_price,
                interval_bar.volume if offset == 0 else 0,
            ))
        minute_groups.append(group)

    for group in minute_groups[:-1]:
        for bar in group:
            runner._persist_warmup_bar("005930", bar)
    for bar in minute_groups[-1]:
        runner._persist_minute_bar("005930", bar)

    index_bars = [
        Bar(datetime(2026, 9, 1, tzinfo=KST) + timedelta(days=index),
            100 + index * 0.1, 100.1 + index * 0.1, 99.9 + index * 0.1,
            100 + index * 0.1, 1)
        for index in range(21)
    ]
    runner.index_bars = {"kospi": index_bars, "kosdaq": index_bars}
    runner._persist_index_snapshot()
    runner._event("SCANNER_CYCLE", "scan-before-decision", {
        "candidate_count": 1,
        "candidates": [{"rank": 1, "symbol": "005930"}],
    }, datetime(2026, 9, 28, 14, 0, tzinfo=KST))
    runner._candidate_rank = {"005930": 1}
    runner._decision("005930", 15, "breakout", completed, Regime.UP, observed)
    runner._index_latest_hash()
    runner._write_manifest()

    decisions = [
        json.loads(line)
        for line in runner.events_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event_type") == "DECISION"
    ]
    assert decisions[0]["decision"] == "ENTER"
    report = verify_shadow_run(runner.directory)
    assert report["status"] == "PASS", json.dumps(report)
    assert report["decision_count"] == 1
    assert report["parity_mismatches"] == 0
    assert report["captured_warmup_rows"] == 20 * 15

    original_bars = runner.bars_path.read_text(encoding="utf-8").splitlines()
    runner.bars_path.write_text("\n".join([*original_bars, original_bars[0]]) + "\n", encoding="utf-8")
    changed = verify_shadow_run(runner.directory)
    assert changed["status"] == "FAIL"
    assert changed["file_checks"]["bars.jsonl"]["status"] == "HASH_MISMATCH"

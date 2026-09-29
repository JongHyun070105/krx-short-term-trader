from __future__ import annotations

from krx_trader.research.phase9_replay import _publish_adjusted_sources


def test_corrected_replay_labels_adjusted_daily_source_without_changing_outcomes() -> None:
    source = [{
        "prior_close_source": "KIS_DAILY_CLOSE_RAW",
        "gap_pct": 2.5,
        "open_to_checkpoint_return_pct": {"10:00": 0.75},
        "longer_horizon_status": {
            "NEXT_SESSION_CLOSE": "CLEAN_RAW_DAILY_HORIZON",
            "THREE_SESSION_CLOSE": "SUSPICIOUS_RAW_PRICE_JUMP_DURING_HORIZON",
        },
    }]

    result = _publish_adjusted_sources(source)

    assert result[0]["prior_close_source"] == "KIS_DAILY_CLOSE_ADJUSTED_REPLAY"
    assert result[0]["gap_pct"] == 2.5
    assert result[0]["open_to_checkpoint_return_pct"] == {"10:00": 0.75}
    assert result[0]["longer_horizon_status"] == {
        "NEXT_SESSION_CLOSE": "CLEAN_ADJUSTED_DAILY_HORIZON",
        "THREE_SESSION_CLOSE": "SUSPICIOUS_RAW_PRICE_JUMP_DURING_HORIZON",
    }
    assert source[0]["prior_close_source"] == "KIS_DAILY_CLOSE_RAW"


def test_corrected_replay_preserves_minute_proxy_source_label() -> None:
    source = [{
        "prior_close_source": "KIS_PREVIOUS_CONTINUOUS_SESSION_15_19_CLOSE_PROXY",
        "longer_horizon_status": {},
    }]
    assert _publish_adjusted_sources(source)[0]["prior_close_source"] == source[0]["prior_close_source"]

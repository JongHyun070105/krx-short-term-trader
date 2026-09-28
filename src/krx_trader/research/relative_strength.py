from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo

from krx_trader.backtest.costs import CostModel
from krx_trader.models import Bar, Decision, Signal
from krx_trader.strategies.relative_strength import (
    DEFAULT_RS_CONFIG,
    CrossSectionalEngine,
    RelativeStrengthConfig,
    RelativeStrengthVariant,
    evaluate_relative_strength,
)

KST = ZoneInfo("Asia/Seoul")
BASE_COST = CostModel(broker_fee_rate=0.00015, sell_tax_rate=0.0020, slippage_bps=15.0)
RANK_BUCKETS = (
    (0.0, 20.0, "0-20"),
    (20.0, 40.0, "20-40"),
    (40.0, 60.0, "40-60"),
    (60.0, 80.0, "60-80"),
    (80.0, 90.0, "80-90"),
    (90.0, 100.01, "90-100"),
)


def _rank_bucket(pct: float) -> str:
    for low, high, label in RANK_BUCKETS:
        if low <= pct < high:
            return label
    return "90-100" if pct >= 100.0 else "UNKNOWN"


def compute_relative_strength_anatomy(
    resampled_bars_by_symbol: dict[str, list[Bar]],
    *,
    config: RelativeStrengthConfig = DEFAULT_RS_CONFIG,
    interval_name: str = "15m",
) -> dict[str, Any]:
    """Examine forward continuation, MFE/MAE, and monotonicity across rank and persistence buckets."""
    timeline = sorted({bar.time for values in resampled_bars_by_symbol.values() for bar in values})
    bars_by_time = {
        symbol: {bar.time: bar for bar in values}
        for symbol, values in resampled_bars_by_symbol.items()
    }
    symbol_timelines = {
        symbol: [bar.time for bar in values]
        for symbol, values in resampled_bars_by_symbol.items()
    }

    engine = CrossSectionalEngine(config)
    histories: dict[str, list[Bar]] = {s: [] for s in resampled_bars_by_symbol}

    # Record all observations with forward outcomes
    observations_with_outcomes: list[dict[str, Any]] = []

    for t in timeline:
        for symbol in sorted(resampled_bars_by_symbol.keys()):
            bar = bars_by_time[symbol].get(t)
            if bar is not None:
                histories[symbol].append(bar)

        obs_dict = engine.evaluate_timestamp(t, histories)
        for symbol, obs in obs_dict.items():
            sym_bars = resampled_bars_by_symbol[symbol]
            times = symbol_timelines[symbol]
            idx = times.index(t)

            # Ensure we have at least 1 next bar for executable entry
            if idx + 1 >= len(sym_bars):
                continue
            entry_open = sym_bars[idx + 1].open
            if entry_open <= 0:
                continue

            # Forward returns from next-bar open to future closes
            ret_fwd_1 = (sym_bars[idx + 1].close - entry_open) / entry_open
            ret_fwd_2 = (sym_bars[idx + 2].close - entry_open) / entry_open if idx + 2 < len(sym_bars) else None
            ret_fwd_4 = (sym_bars[idx + 4].close - entry_open) / entry_open if idx + 4 < len(sym_bars) else None
            ret_fwd_8 = (sym_bars[idx + 8].close - entry_open) / entry_open if idx + 8 < len(sym_bars) else None

            # 10-bar forward window
            fwd_window = sym_bars[idx + 1: min(len(sym_bars), idx + 11)]
            mfe = max((b.high - entry_open) / entry_open for b in fwd_window)
            mae = min((b.low - entry_open) / entry_open for b in fwd_window)

            observations_with_outcomes.append({
                "timestamp": t.isoformat(),
                "symbol": symbol,
                "percentile_short": obs.percentile_short,
                "percentile_med": obs.percentile_med,
                "ret_short": obs.ret_short,
                "ret_med": obs.ret_med,
                "ret_1bar": obs.ret_1bar,
                "persistence_count": obs.persistence_count,
                "cohort_size": obs.cohort_size,
                "median_ret_med": obs.median_ret_med,
                "ret_fwd_1": ret_fwd_1,
                "ret_fwd_2": ret_fwd_2,
                "ret_fwd_4": ret_fwd_4,
                "ret_fwd_8": ret_fwd_8,
                "mfe_10bar": mfe,
                "mae_10bar": mae,
                "continuation_1bar": ret_fwd_1 > 0,
                "continuation_2bar": ret_fwd_2 > 0 if ret_fwd_2 is not None else False,
                "continuation_4bar": ret_fwd_4 > 0 if ret_fwd_4 is not None else False,
            })

    # Bucket analysis
    by_rank_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_persistence: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for item in observations_with_outcomes:
        bucket = _rank_bucket(item["percentile_short"])
        by_rank_bucket[bucket].append(item)
        by_persistence[item["persistence_count"]].append(item)

    def summarize_slice(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {"sample_size": 0}
        fwd_1 = [it["ret_fwd_1"] for it in items]
        fwd_2 = [it["ret_fwd_2"] for it in items if it["ret_fwd_2"] is not None]
        fwd_4 = [it["ret_fwd_4"] for it in items if it["ret_fwd_4"] is not None]
        mfes = [it["mfe_10bar"] for it in items]
        maes = [it["mae_10bar"] for it in items]

        return {
            "sample_size": len(items),
            "mean_fwd_1bar_return": statistics.mean(fwd_1),
            "mean_fwd_2bar_return": statistics.mean(fwd_2) if fwd_2 else None,
            "mean_fwd_4bar_return": statistics.mean(fwd_4) if fwd_4 else None,
            "win_rate_1bar": sum(r > 0 for r in fwd_1) / len(fwd_1),
            "win_rate_2bar": sum(r > 0 for r in fwd_2) / len(fwd_2) if fwd_2 else None,
            "win_rate_4bar": sum(r > 0 for r in fwd_4) / len(fwd_4) if fwd_4 else None,
            "mean_mfe_10bar": statistics.mean(mfes),
            "mean_mae_10bar": statistics.mean(maes),
            "mfe_mae_ratio": statistics.mean(mfes) / max(1e-6, abs(statistics.mean(maes))),
            "continuation_rate_1bar": sum(it["continuation_1bar"] for it in items) / len(items),
            "continuation_rate_2bar": sum(it["continuation_2bar"] for it in items) / len(items),
            "continuation_rate_4bar": sum(it["continuation_4bar"] for it in items) / len(items),
        }

    rank_bucket_summary = {
        label: summarize_slice(by_rank_bucket[label])
        for _, _, label in RANK_BUCKETS
    }

    persistence_summary = {
        f"{p}_of_{config.persistence_lookback}": summarize_slice(by_persistence[p])
        for p in range(config.persistence_lookback + 1)
    }

    # Check monotonicity of rank buckets
    top_bucket = rank_bucket_summary.get("90-100", {})
    bottom_bucket = rank_bucket_summary.get("0-20", {})
    rank_monotonic = (
        top_bucket.get("sample_size", 0) > 0
        and bottom_bucket.get("sample_size", 0) > 0
        and top_bucket.get("mean_fwd_1bar_return", -999) > bottom_bucket.get("mean_fwd_1bar_return", 999)
    )

    # Check persistence advantage: 3/3 vs 1/3
    p3 = persistence_summary.get(f"{config.persistence_lookback}_of_{config.persistence_lookback}", {})
    p1 = persistence_summary.get(f"1_of_{config.persistence_lookback}", {})
    persistence_advantage = (
        p3.get("sample_size", 0) > 0
        and p1.get("sample_size", 0) > 0
        and p3.get("mean_fwd_1bar_return", -999) > p1.get("mean_fwd_1bar_return", 999)
    )

    return {
        "interval": interval_name,
        "total_observations": len(observations_with_outcomes),
        "total_timestamps": len(timeline),
        "total_symbols": len(resampled_bars_by_symbol),
        "rank_bucket_summary": rank_bucket_summary,
        "persistence_summary": persistence_summary,
        "monotonicity_rank_fwd_return": bool(rank_monotonic),
        "persistence_advantage_over_spike": bool(persistence_advantage),
    }


def simulate_relative_strength_signals(
    resampled_bars_by_symbol: dict[str, list[Bar]],
    *,
    variant: RelativeStrengthVariant = RelativeStrengthVariant.RS_A,
    config: RelativeStrengthConfig = DEFAULT_RS_CONFIG,
    cost_model: CostModel = BASE_COST,
    stress_multiplier: float = 1.0,
) -> dict[str, Any]:
    """Run chronological simulation of RS strategy with exact next-bar execution and bounded holding."""
    timeline = sorted({bar.time for values in resampled_bars_by_symbol.values() for bar in values})
    bars_by_time = {
        symbol: {bar.time: bar for bar in values}
        for symbol, values in resampled_bars_by_symbol.items()
    }
    symbol_timelines = {
        symbol: [bar.time for bar in values]
        for symbol, values in resampled_bars_by_symbol.items()
    }

    engine = CrossSectionalEngine(config)
    histories: dict[str, list[Bar]] = {s: [] for s in resampled_bars_by_symbol}

    signals: list[Signal] = []
    trade_outcomes: list[dict[str, Any]] = []

    for t in timeline:
        for symbol in sorted(resampled_bars_by_symbol.keys()):
            bar = bars_by_time[symbol].get(t)
            if bar is not None:
                histories[symbol].append(bar)

        obs_dict = engine.evaluate_timestamp(t, histories)

        for symbol in sorted(resampled_bars_by_symbol.keys()):
            bar = bars_by_time[symbol].get(t)
            if bar is None:
                continue
            obs = obs_dict.get(symbol)
            sig = evaluate_relative_strength(
                obs,
                histories[symbol],
                symbol,
                variant=variant,
                config=config,
            )
            if sig.decision == Decision.ENTER:
                signals.append(sig)

                # Simulate execution on subsequent bars
                sym_bars = resampled_bars_by_symbol[symbol]
                times = symbol_timelines[symbol]
                idx = times.index(t)

                if idx + 1 >= len(sym_bars):
                    continue  # Cannot enter after final bar

                entry_bar = sym_bars[idx + 1]
                entry_price = entry_bar.open
                stop_price = sig.stop_price or 0.0

                effective_slippage = (cost_model.slippage_bps * stress_multiplier) / 10_000.0
                effective_fee = cost_model.broker_fee_rate * stress_multiplier
                effective_tax = cost_model.sell_tax_rate * stress_multiplier

                buy_cost = (1.0 + effective_slippage) * (1.0 + effective_fee)
                actual_entry = entry_price * buy_cost

                # Hold for up to max_holding_bars
                exit_price = entry_price
                exit_reason = "HOLDING_EXPIRED"
                bars_held = 0
                mfe_p = 0.0
                mae_p = 0.0
                time_to_mfe = 0
                time_to_stop = 0

                future_bars = sym_bars[idx + 1: min(len(sym_bars), idx + 1 + config.max_holding_bars)]
                for b_i, f_bar in enumerate(future_bars, 1):
                    bars_held = b_i
                    high_gain = (f_bar.high - entry_price) / entry_price
                    low_gain = (f_bar.low - entry_price) / entry_price

                    if high_gain > mfe_p:
                        mfe_p = high_gain
                        time_to_mfe = b_i
                    mae_p = min(mae_p, low_gain)

                    if stop_price > 0 and f_bar.low <= stop_price:
                        exit_price = min(f_bar.open, stop_price) if f_bar.open <= stop_price else stop_price
                        exit_reason = "STOP_HIT"
                        time_to_stop = b_i
                        break
                    exit_price = f_bar.close

                sell_cost = (1.0 - effective_slippage) * (1.0 - effective_fee - effective_tax)
                actual_exit = exit_price * sell_cost

                gross_ret = (exit_price - entry_price) / entry_price
                net_ret = (actual_exit - actual_entry) / actual_entry

                # Rank decay: track rank at +1, +2, +3 if available
                rank_at_sig = obs.percentile_short if obs else 0.0
                rank_p1 = None
                rank_p2 = None
                rank_p3 = None
                if idx + 1 < len(sym_bars):
                    t1 = sym_bars[idx + 1].time
                    obs1 = engine.evaluate_timestamp(t1, histories).get(symbol)
                    rank_p1 = obs1.percentile_short if obs1 else None
                if idx + 2 < len(sym_bars):
                    t2 = sym_bars[idx + 2].time
                    obs2 = engine.evaluate_timestamp(t2, histories).get(symbol)
                    rank_p2 = obs2.percentile_short if obs2 else None
                if idx + 3 < len(sym_bars):
                    t3 = sym_bars[idx + 3].time
                    obs3 = engine.evaluate_timestamp(t3, histories).get(symbol)
                    rank_p3 = obs3.percentile_short if obs3 else None

                trade_outcomes.append({
                    "timestamp": t.isoformat(),
                    "symbol": symbol,
                    "entry_time": entry_bar.time.isoformat(),
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "bars_held": bars_held,
                    "gross_return": gross_ret,
                    "net_return": net_ret,
                    "win": net_ret > 0,
                    "mfe": mfe_p,
                    "mae": mae_p,
                    "time_to_mfe": time_to_mfe,
                    "time_to_stop": time_to_stop,
                    "cost_drag": gross_ret - net_ret,
                    "rank_at_signal": rank_at_sig,
                    "rank_plus_1": rank_p1,
                    "rank_plus_2": rank_p2,
                    "rank_plus_3": rank_p3,
                    "session_date": t.date().isoformat(),
                })

    n_trades = len(trade_outcomes)
    if n_trades == 0:
        return {
            "variant": variant.value,
            "signals": len(signals),
            "trades": 0,
            "win_rate": 0.0,
            "gross_expectancy": 0.0,
            "net_expectancy": 0.0,
            "profit_factor": 0.0,
            "trades_detail": [],
        }

    wins = [tr for tr in trade_outcomes if tr["net_return"] > 0]
    losses = [tr for tr in trade_outcomes if tr["net_return"] <= 0]
    gross_pnl = sum(tr["gross_return"] for tr in trade_outcomes)
    net_pnl = sum(tr["net_return"] for tr in trade_outcomes)
    net_wins = sum(tr["net_return"] for tr in wins)
    net_losses = abs(sum(tr["net_return"] for tr in losses))

    pf = net_wins / max(1e-6, net_losses) if net_losses > 0 else (99.0 if net_wins > 0 else 0.0)
    win_rate = len(wins) / n_trades

    # Rank decay analysis
    w_p1 = [tr["rank_plus_1"] for tr in wins if tr.get("rank_plus_1") is not None]
    w_p2 = [tr["rank_plus_2"] for tr in wins if tr.get("rank_plus_2") is not None]
    l_p1 = [tr["rank_plus_1"] for tr in losses if tr.get("rank_plus_1") is not None]
    l_p2 = [tr["rank_plus_2"] for tr in losses if tr.get("rank_plus_2") is not None]

    winners_rank_decay = {
        "signal": statistics.mean([tr["rank_at_signal"] for tr in wins]) if wins else 0.0,
        "plus_1": statistics.mean(w_p1) if w_p1 else 0.0,
        "plus_2": statistics.mean(w_p2) if w_p2 else 0.0,
    }
    losers_rank_decay = {
        "signal": statistics.mean([tr["rank_at_signal"] for tr in losses]) if losses else 0.0,
        "plus_1": statistics.mean(l_p1) if l_p1 else 0.0,
        "plus_2": statistics.mean(l_p2) if l_p2 else 0.0,
    }

    # Concentration analysis
    sorted_trades = sorted(trade_outcomes, key=lambda tr: tr["net_return"], reverse=True)
    top1_share = sorted_trades[0]["net_return"] / max(1e-6, net_pnl) if net_pnl > 0 else 0.0
    top5_share = sum(tr["net_return"] for tr in sorted_trades[:5]) / max(1e-6, net_pnl) if net_pnl > 0 else 0.0

    by_symbol_pnl: dict[str, float] = defaultdict(float)
    for tr in trade_outcomes:
        by_symbol_pnl[tr["symbol"]] += tr["net_return"]
    sorted_syms = sorted(by_symbol_pnl.items(), key=lambda x: x[1], reverse=True)
    top_sym_share = sorted_syms[0][1] / max(1e-6, net_pnl) if net_pnl > 0 and sorted_syms else 0.0

    # Monthly breakdown
    by_month_pnl: dict[str, list[float]] = defaultdict(list)
    for tr in trade_outcomes:
        m = tr["session_date"][:7]
        by_month_pnl[m].append(tr["net_return"])
    monthly_summary = {
        m: {
            "trades": len(rets),
            "net_pnl": sum(rets),
            "win_rate": sum(r > 0 for r in rets) / len(rets),
        }
        for m, rets in sorted(by_month_pnl.items())
    }

    return {
        "variant": variant.value,
        "signals": len(signals),
        "trades": n_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "gross_expectancy": gross_pnl / n_trades,
        "net_expectancy": net_pnl / n_trades,
        "profit_factor": pf,
        "mean_mfe": statistics.mean(tr["mfe"] for tr in trade_outcomes),
        "mean_mae": statistics.mean(tr["mae"] for tr in trade_outcomes),
        "mean_time_to_mfe": statistics.mean(tr["time_to_mfe"] for tr in trade_outcomes),
        "mean_time_to_stop": statistics.mean(tr["time_to_stop"] for tr in trade_outcomes if tr["time_to_stop"] > 0) if any(tr["time_to_stop"] > 0 for tr in trade_outcomes) else 0.0,
        "mean_cost_drag": statistics.mean(tr["cost_drag"] for tr in trade_outcomes),
        "rank_decay": {
            "winners": winners_rank_decay,
            "losers": losers_rank_decay,
        },
        "concentration": {
            "top1_trade_share": top1_share,
            "top5_trades_share": top5_share,
            "top_symbol_share": top_sym_share,
            "top_symbol": sorted_syms[0][0] if sorted_syms else None,
        },
        "monthly_summary": monthly_summary,
        "stress_multiplier": stress_multiplier,
        "trades_detail": trade_outcomes,
    }

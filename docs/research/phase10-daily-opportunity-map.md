# Phase 10 Daily Opportunity Map

## Entry gate and source

Phase 10 runs only when the Phase 9 summary permits it and the Phase 9 artifact index verifies cleanly. It reads one price convention: KIS inquire-daily-itemchartprice with FID_ORG_ADJ_PRC=0 (adjusted), from the separate Phase 9 reconciliation cache. It does not read the existing multi-session daily cache, minute cache, Secondary period, External block outcomes, or the July–August protected Holdout.

The only pre-Development input is the immediately preceding daily bar, when present, to define the first safe Development one-day return. It is used only as a trailing feature reference. No pre-Development observation or outcome is emitted. Every signal observation is dated April 17 through June 30, 2026, and every calculated outcome remains within that same Development interval.

## Observation and outcome definitions

For each frozen cohort symbol and completed Development day D, features use bars no later than D: 1/2/3/5-session returns; 3/5-session range, realized volatility and close location; distance from the recent 5-session high/low; volume ratio against the prior five-session median; adjusted close and adjusted-close-times-volume liquidity proxy; market; and same-day cross-sectional percentiles. Volatility terciles and return/volatility ranks are calculated within each date, so later Development sessions cannot change earlier cross-sectional ranks.

Outcomes assume the signal is known after D closes. Entry uses the next session open, and horizon h exits at the close of session D+h. The 1-session result is context; the map centers on 2, 3 and 5 sessions. MFE and MAE cover the entry session through the exit session. No D close is treated as an executable fill.

The primary map uses fixed coarse 3-session return buckets and the 2/3/5-session horizons. Separate descriptive tables cover 5-session return buckets, same-day LOW/MID/HIGH 5-session realized-volatility states, recent-range position, coarse adjusted-price buckets, and coarse adjusted-close-times-volume buckets. KOSPI/KOSDAQ and April/May/June results remain visible separately. There is no parameter search or machine learning.

## Costs and promotion gate

The assumed round-trip friction is 0.53%: 0.015% fee per side, 0.20% sell tax, and 15 bps slippage per side. Reports show 1×, 1.5× and 2× cost sensitivity. The single-position-per-symbol execution view prevents overlapping observations for a proposed family from inflating its sample.

A Development family needs at least 30 non-overlapping trades, at least 1% gross mean, positive net mean at 1× and 2× costs, payoff ratio above 1, at least two positive months with five or more observations each, no symbol or signal day contributing more than 20% of its trades, and no opposite KOSPI/KOSDAQ mean where both markets have at least 10 trades. Only one qualifying family can become a research candidate. It must still pass the frozen Secondary one-shot before preregistration or External evaluation.

## Artifacts

Runtime output is confined to runtime/research/phase10/: daily-features.csv, daily-outcomes.csv, phase10-opportunity-map.json, phase10-summary.json, phase10-report.md, phase10-artifact-index.json, and artifact-integrity.json. The index records each data/report artifact's size and SHA-256. The integrity report is kept separately because an index cannot contain its own final hash.

The adjusted-close-times-volume value is a descriptive liquidity proxy; corporate actions can make historical adjusted-price turnover differ from cash turnover. Minute adjustment remains unknown, and neither outcome mapping nor this policy proves alpha or authorizes shadow, paper, or live operation.

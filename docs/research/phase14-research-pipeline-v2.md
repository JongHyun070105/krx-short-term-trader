# Phase 14 — Research Pipeline v2 and Pre-Strategy Factor Viability

## Decision and boundary

Phase 14 implemented and executed a strategy-free factor research pipeline on the existing Phase 13 safe panel. The analysis window was 2023-01-02 through 2025-06-30. It read 164,733 stock/horizon records; the latest forward exit was 2025-06-30. The 2025 H2 Confirmation, 2026 External, Phase 10 2026 Development, and 2026-07-28 through 2026-08-28 Holdout were not inputs.

**Decision:** `PIPELINE_V2=PASS`; `RESEARCH_LEDGER=COMPLETE`; `BEST_FACTOR_FAMILY=NONE`; `BEST_FACTOR_VIABILITY=NONE`; `EXISTING_FACTOR_INFORMATION=WEAK`; `NEXT_RESEARCH_MODE=NEW_INFORMATION_SOURCE`. Nine factor families had at least one non-zero, selection-aware IC surface, but no family passed the complete pre-strategy viability gate. `ALPHA=UNPROVEN`, `LIVE=DISABLED`, and `SHADOW_NEXT_SESSION=NO` remain unchanged. No strategy, Phase 14 candidate, entry/exit rule, sizing logic, broker path, Confirmation run, or External evaluation was created.

The Phase 13 interaction family was `WEAK` on evidence ending before Confirmation, so v2 would have blocked its candidate from reaching Confirmation. That is a retrospective challenge result, not evidence that v2 is prospectively calibrated.

## Frozen run identity

| Item | Value |
|---|---|
| Starting SHA | `160fb16419f33be45efeac7e48ab4b1eda444661` |
| Analysis source SHA | `ef7a79128bdc596d715f3ddbdcc29350c3795ce5` |
| Frozen config SHA-256 | `88dcb008acee28dfdac087773a24d392049930e044c1cf88407cdb7f9d5bd4e7` |
| Factor registry SHA-256 | `557bd10155caef63b696841d6893da3b07e1b6651b587c385cd79dbb3da128eb` |
| Safe-panel dataset SHA-256 | `b7b07f60352d4b8d52d3917f6af35bb3d377b313428db30867ce79d2c67fd3f5` |
| Seed / cross-sectional permutations | `20261001` / `1,000` per factor family |
| Time-dislocation permutations | `500` |
| Date-cluster bootstrap draws | `2,000` |
| NumPy RNG | `2.5.3`, `Generator(PCG64)` |
| Round-trip cost screen | `0.53%` |
| Targets / horizons | Absolute, simple excess, beta residual / 3, 5, 10 sessions |

The pipeline config and registry were frozen and hashed before the retrospective audit. After the first execution exposed short-history symbols in the secondary time-dislocation diagnostic, its eligibility rule was added and the config was re-frozen before the audit: symbols with 120 or fewer observations are excluded from that diagnostic and counted. No viability threshold, cost threshold, stability rule, or null significance criterion changed after the audit.

## Research ledger and evidence periods

The ledger records a minimum of **9 strategy families**, **11 factor families**, and **4 interaction families** (24 named family labels combined). At least two one-shot candidate rules are explicitly staged in the Phase 11 and Phase 13 records; this is a lower bound because earlier exploratory variants cannot be exhaustively recovered. Historical work examined at least two directional interpretations, two market splits, and forward horizons 1, 2, 3, 5, 10, and 20 sessions.

| Period | Classification | Phase 14 use |
|---|---|---|
| 2023-01-02–2024-06-28 | `HEAVILY_TOUCHED_DISCOVERY` | Used; not independent |
| 2024-07-01–2025-06-30 | `HEAVILY_TOUCHED_REPLICATION` | Used; not independent |
| 2025-07-01–2025-12-30 | `CONSUMED_BY_PHASE13_CONFIRMATION` | Excluded from features, targets, and nulls; Phase 13 outcome is appended afterward as a challenge label |
| 2026-01-05–2026-04-16 | `UNREAD_EXTERNAL` | Not read; one unread block remains |
| 2026-04-17–2026-06-30 | Phase 10 development, touched | Excluded; outside the Phase 13 safe panel |
| 2026-07-28–2026-08-28 | `OUTCOME_UNREAD_METADATA_EXPOSED` | Prices, metadata contents, factors, signals, and outcomes not read; not pristine |

There are **zero genuinely independent historical periods remaining**. One unread External block remains, but the project has not specified a new `PROSPECTIVE_UNREAD` evidence window. The Holdout remains outcome-unread but is not described as pristine because its metadata was exposed previously.

The historical family inventory includes Breakout, Pullback, Retest, Relative Strength Continuation, Mean Reversion, VWAP Reclaim, Opening Gap, Daily Opportunity, Market Stress Rebound, Residual/Excess, and turnover/liquidity interactions. The registry reuses the Phase 13 panel's available factor definitions; it does not reconstruct unavailable intraday or investor-flow factors.

## Opportunity surface

Unconditional stock observations have large movement and dispersion relative to 0.53% friction. These are opportunity-scale diagnostics, not factor edges or strategy returns.

| Target | Horizon | Median return | Median absolute move | Mean P90–P10 cross-sectional spread | P90 absolute move | Typical / spread / upper-tail to cost |
|---|---:|---:|---:|---:|---:|---:|
| Absolute | 3 | −0.168% | 2.153% | 8.454% | 7.337% | 4.06 / 15.95 / 13.84× |
| Simple excess | 3 | −0.303% | 2.070% | 8.454% | 6.644% | 3.91 / 15.95 / 12.54× |
| Beta residual | 3 | −0.254% | 1.933% | 8.226% | 6.495% | 3.65 / 15.52 / 12.25× |
| Absolute | 5 | −0.200% | 2.878% | 11.112% | 9.616% | 5.43 / 20.97 / 18.14× |
| Simple excess | 5 | −0.451% | 2.739% | 11.112% | 8.678% | 5.17 / 20.97 / 16.37× |
| Beta residual | 5 | −0.363% | 2.539% | 10.872% | 8.430% | 4.79 / 20.51 / 15.91× |
| Absolute | 10 | −0.355% | 4.167% | 16.242% | 13.853% | 7.86 / 30.65 / 26.14× |
| Simple excess | 10 | −0.816% | 4.023% | 16.242% | 12.483% | 7.59 / 30.65 / 23.55× |
| Beta residual | 10 | −0.674% | 3.720% | 15.791% | 12.223% | 7.02 / 29.79 / 23.06× |

There were 54,155–55,478 stock observations and 587–601 unique signal dates per target/horizon. The target surface has ample raw movement; the missing ingredient is a broad, stable factor relationship whose absolute and market-adjusted effects survive selection control and cost gates.

## Factor registry and researcher degrees of freedom

The frozen registry contains **63 factor definitions in 14 semantic families**: raw trailing return; market excess return; beta residual return; residual drawdown; idiosyncratic volatility; abnormal traded-value activity; residual cross-sectional rank; range position; market context; breadth; dispersion; and the three historical Phase 13 interaction families. A separate SHA-256 stable symbol/date pseudo-rank is the negative control.

The search surface counted **118 directional interpretations**, three horizons, three targets, and pooled/KOSPI/KOSDAQ scopes: 3,186 raw directional metric cells, 8,505 fixed quantile-state cells, 39 historical interaction-state variants, and 3,225 candidate-selection opportunities under the ledger's stated definition. The effective family count is the 14 frozen semantic families; it is not a correlation-estimated count. No formal cross-family FDR/FWER correction was implemented. The within-family max-statistic null is the primary selection-aware control and all raw counts remain visible.

## Predictability, quantiles, and walk-forward stability

The daily Spearman IC uses factor(T) against absolute, simple-excess, and beta-residual outcomes at 3/5/10 sessions. `ALL` averages the date's KOSPI and KOSDAQ rank ICs; uncertainty clusters by signal date. Quantile tables preserve all five broad quantiles, and the monotonicity summary reports quantile-number rank correlation, adjacent inversions, and extreme-versus-middle consistency.

- Families with at least one oriented mean IC of at least 0.02: abnormal traded-value activity, beta residual return, idiosyncratic volatility, market excess return, all three Phase 13 interaction families, range position, and residual cross-sectional rank.
- Largest absolute ICs were the 10-session idiosyncratic-volatility relationship: −0.104 for absolute return and simple excess in KOSDAQ, and −0.098 for beta residual in KOSDAQ. These are negative-direction descriptive results, not long-only alpha claims.
- Families with measured broad monotonic quantile structure: beta residual return and residual cross-sectional rank. Other large ICs did not show the required broad ordered structure.
- Directionally positive in at least four of the five fixed chronological blocks: beta residual return, idiosyncratic volatility, market excess return, range position, and residual cross-sectional rank. Each passed 4/5 blocks; no single strong half-year rescued the weaker families.
- The Phase 13 excess-turnover family had positive oriented IC in the chosen surface but zero positive quantile-spread blocks. Its strongest selected state was not the historically proposed `LE_-4 × EXPANDED` cell, and its family verdict was only `WEAK`.

All block-level ICs, Q5−Q1/Q1−Q5 spreads, quantile means, and inversions are in `factor-predictability.json`, `quantile-spreads.json`, `factor-monotonicity.json`, and `walk-forward-stability.json`.

## Null models and multiple-testing control

The primary null permutes factor ranks across eligible symbols inside each market/date group, preserving each date's target cross-section and market shock. Every permutation searches the same family variants, both declared orientations, three targets, three horizons, and three market scopes, then records the family maximum. The deterministic seed is `20261001`; each family used 1,000 permutations. The result hash is recorded per family.

Nine families had empirical family-max p=`1/1001` (zero exceedances at this resolution): abnormal traded-value activity, beta residual, idiosyncratic volatility, market excess return, the three Phase 13 interactions, range position, and residual cross-sectional rank. Raw trailing return and residual drawdown had p=1.0; constant market-context, breadth, and dispersion ranks also had p=1.0. Family maxima must not be read as cross-family-adjusted p-values.

For example, the idiosyncratic-volatility family observed max was 0.1005 versus null median 0.0108 and P95 0.0173. The Phase 13 excess-turnover family observed max was 0.0680 versus null median 0.0162 and P95 0.0209. These exceedance results establish selection-aware cross-sectional association in the touched panel; they do not establish causality, independent validation, or profitability.

The secondary circular-shift null used the raw 5-session return factor and 5-session absolute return. It ran 500 shifts with minimum offset 60 observations, retained 94 symbols with more than 120 observations, excluded two short-history symbols, and used no protected rows. Observed within-symbol mean Spearman was −0.0572; null median −0.0066, P95 +0.0055, and one-sided positive-orientation p=1.0. This does not support the frozen positive momentum direction.

The deterministic SHA-256 negative control failed viability (`FAIL`): observed family max 0.0128, empirical p 0.275, and 0/1 full-pipeline promotions. Across its 1,000 randomized family surfaces, 1.6% reached an apparently attractive max IC of at least 0.02. One control is a sanity check, not a precise estimate of false-promotion frequency.

## Economic scale, market adjustment, and concentration

All factor families received a cost screen before any synthesis. Only idiosyncratic volatility had a selected absolute long-only effect above 0.53%: +0.592%, 1.12× cost, from 743 observations across 587 dates. Its oriented simple-excess spread was +1.101%, beta-residual spread +0.977%, matched-market share 35.7%, and beta-dominated gate `NO`. It still failed quantile monotonicity and therefore remained `WEAK`.

Market excess return was beta dominated (market share 183.5%, `YES`). Other selected family long-only scales were below cost or negative: abnormal activity −0.165% (−0.31×), residual return +0.040% (0.08×), market excess +0.115% (0.22×), range position +0.014% (0.03×), raw return +0.514% (0.97×), and residual drawdown +0.174% (0.33×). Factor-level market-adjusted, absolute, simple-excess, and beta-residual results are all retained, including `UNCERTAIN` where beta coverage or a selected state was insufficient.

For populated selected states, date concentration was modest for continuous families: the top five dates contributed 4.4–5.5% of positive selected-quantile return and the top three symbols 15–28%. The residual-drawdown × high-idiosyncratic-volatility cell used only 26 date clusters; its top five dates were 49.9% and top three symbols 35.4%, near the fixed limits. Several interaction families had no selected long-only observations after their max-IC state was oriented; their concentration/economic estimates are unavailable and cannot pass the gate. Cluster bootstrap samples whole signal dates, never stock rows; the selected family estimates use 2,000 draws.

## Factor viability

The table below gives final family states. `WEAK` means some structure survived a subset of screens; it does not mean a candidate may be created.

| Factor family | V2 state | Main failed dimensions / reason |
|---|---|---|
| Abnormal traded-value activity | `WEAK` | Walk-forward, monotonicity, absolute scale, market-adjusted gate |
| Beta residual return | `WEAK` | Absolute scale and market-adjusted gate |
| Idiosyncratic volatility | `WEAK` | Monotonicity; the only cost-sized family still lacked ordered quantiles |
| Market breadth | `FAIL` | No eligible independent cross-sectional predictive structure |
| Market context | `FAIL` | Conditioning-only; cross-sectional IC surface is zero |
| Market excess dispersion | `FAIL` | Conditioning-only; cross-sectional IC surface is zero |
| Market excess return | `WEAK` | Absolute scale, market-beta dominance, monotonicity |
| Phase 13 excess × turnover | `WEAK` | No broad monotonicity or directional block stability; selected state lacked sufficient long-only coverage |
| Phase 13 residual drawdown × idiosyncratic volatility | `WEAK` | Sample depth, economics, market-adjusted edge, stability, and concentration |
| Phase 13 residual rank × turnover | `WEAK` | No broad monotonicity, stability, or adequate selected-state sample |
| Range position | `WEAK` | Absolute scale, market-adjusted effect, monotonicity |
| Raw trailing return | `FAIL` | Family max did not separate from null; no stable broad direction |
| Residual cross-sectional rank | `WEAK` | Absolute scale and market-adjusted edge |
| Residual drawdown | `FAIL` | Family max did not separate from null; no stable selected relationship |

Every family retains `SURVIVORSHIP_RISK=HIGH`, `DATA_QUALITY=PARTIAL`, and the warnings `POINT_IN_TIME_UNIVERSE=PARTIAL`, `PIT_PRICE_HISTORY=NOT_AVAILABLE`, `SURVIVORSHIP_SENSITIVITY=NOT_TESTABLE`, and `ADJUSTED_PRICE_REVISION_RISK=PRESENT`. No family is `PROMISING_FOR_FUTURE_STUDY`.

## Retrospective audit and next information source

The frozen Phase 13 interaction family was `WEAK` on pre-Confirmation inputs, so v2 would have blocked the candidate from Confirmation. Only after that verdict was recorded was the existing challenge label appended: Phase 13 Confirmation gross −0.928790%, net at 1× −1.458790%, simple excess −1.489630%, and beta residual −1.252423%. No confirmation rows were inputs to V2 metrics, nulls, config, or thresholds; no post-hoc gates changed. `PIPELINE_FALSE_POSITIVE_AUDIT=PASS` means this historical candidate was blocked by v2.

The selected historical audit also classified Phase 11 market-stress/market-context as `FAIL`, standalone Phase 13 residual reversal as `WEAK`, and Phase 10 daily price states as `FAIL`. All would have stopped before candidate research under the Phase 14 promotion rule. These mappings are retrospective family audits, not rewrites of the earlier phase verdicts.

The current price/liquidity set contains some predictive rank structure and one cost-sized absolute effect, but not the combination of broad monotonicity, stability, market-adjusted edge, and independent evidence required for future strategy synthesis. Recommend a later **new point-in-time information source** phase (for example reproducible investor flow, order-book/microstructure, or timestamp-safe corporate-event information). Phase 14 did not acquire or implement any new source.

## Integrity and status

- Regression: 297 tests at Phase 13 baseline; **323 passed** after Phase 14 (26 added).
- `ruff check src tests`: PASS; `git diff --check`: PASS.
- Phase 14 artifact index verifies 25 payload files; `artifact-integrity.json=PASS` with no failed paths.
- Phase 5–13 snapshot: **38 files unchanged**, `PREVIOUS_PHASE_ARTIFACT_IMMUTABILITY=PASS`; snapshot SHA-256 `80e643a04c6db6eeeff04983add80b89e50befa8f09373b6fc696e19578ff455`.
- `EXTERNAL_2026=NOT_READ`; `HOLDOUT_2026=NOT_READ`; `PHASE13_CONFIRMATION_INPUT_ROWS=0`.
- Strategy synthesis, candidate creation, broker path, private exchange API, and trading were not performed.

Machine-readable Phase 14 artifacts are in `runtime/research/phase14/`. See `phase14-summary.json` for Q1–Q18, `factor-viability.json` for all family dimensions, and `phase14-artifact-index.json` / `artifact-integrity.json` for hashes.

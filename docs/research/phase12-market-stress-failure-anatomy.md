# Phase 12 — Market Stress Rebound Failure Anatomy

## Decision

```text
PHASE11_CANDIDATE = REJECTED
PHASE11_VALIDATION = FAIL
PHASE12_FAILURE_ANATOMY = COMPLETE
MARKET_BETA_EXPLANATION = STRONG
STRESS_COMPOSITION_SHIFT = STRONG
MARKET_STRESS_MECHANISM = NOT_SUPPORTED
PHASE12_CANDIDATE = NOT_CREATED
CONFIRMATION = NOT_RUN
EXTERNAL_2026 = NOT_READ
HOLDOUT_2026 = NOT_READ
SHADOW_NEXT_SESSION = NO
ALPHA = UNPROVEN
LIVE = DISABLED
```

The Development-to-Validation decline is predominantly a weaker matched-index rebound, accompanied by a different volatility/dispersion mix. Stock returns exceeded the matched index by only 0.153% in Development and 0.113% in touched Validation before costs. The simple beta-adjusted residual is diagnostic, not factor alpha, and is below the unchanged 0.53% round-trip cost in both periods. No one-feature mechanism passed the predeclared episode, market-adjusted scale, both-period, and concentration gates. The rejected Phase 11 rule was not altered, and no Confirmation data was opened.

## Scope, inputs, and integrity

| Item | Phase 12 value |
|---|---|
| Starting Git SHA | `dd762564b4ce010f3c31a9e8cf37dab7e973a1c3` |
| Development | 2023-01-02–2024-06-28 |
| Touched Validation (mechanism research only) | 2024-07-01–2025-06-30 |
| Confirmation | 2025-07-01–2025-12-30, not read |
| External 2026 | 2026-01-05–2026-04-16, not read |
| Protected Holdout | 2026-07-28–2026-08-28, not read |
| Cohort | 100 frozen current listings: 50 KOSPI, 50 KOSDAQ |
| Phase 11 dense multi-year coverage | 90 symbols |
| Symbols with rows in the bounded Phase 12 input | 96/100; 4 without rows in this input window |
| Price convention | KIS adjusted daily OHLCV, `FID_ORG_ADJ_PRC=0` |
| Investor-flow panel | `NOT_AVAILABLE` |
| Cohort SHA-256 | `5bcad330613b94bda148401cddcba8e62262a96e3e38acd4b70e4ed013e78f95` |
| Bounded dataset SHA-256 | `38734f06adb539ed5959b03cf4d22da139773429103bc3c2a588737c59f996cf` |
| Config SHA-256 | `e05dae8fe6bea8b10525a4c1f49274a86a3770bd2ad4cd404ccd5a4de0e3d2cf` |
| Prior Phase 5–11 manifest/index snapshot | 23 files; SHA-256 `acd09332adfb9603ebd4ea647389c8757958361519436c95ba3f16154e1201ea` |

The Phase 12 loader materializes completed bars only from 2022-11-01 through 2025-06-30 (warmup plus the touched periods). Input digests are calculated from those bounded rows, so later rows do not enter the Phase 12 dataset digest. Missing prices and features remain missing; no synthetic bars or forward-fill were used. Breadth includes the eligible count, observed count, coverage ratio, market, and date for every observation. The minimum primary coverage ratio is 80%; no market-signal observation fell below it (104/104 Development and 114/114 Validation passed). Mean coverage was 88.9% and 91.9%, respectively. KOSDAQ coverage was lower than KOSPI because unobserved cohort symbols are excluded from the denominator for that observation.

**CURRENT-LISTING SURVIVORSHIP BIAS** applies. This is not a point-in-time historical universe, does not include delisted listings, and cannot support a market-wide unbiased-history claim. The 96 loaded symbols are not a replacement cohort; the frozen 100-symbol cohort and its missing observations were retained. Phase 11's historical status remains unchanged.

The initial 252-test suite, Ruff, and `git diff --check` passed before implementation. Phase 12 added 16 targeted tests covering guards, breadth, episodes, decomposition, beta, deterministic ordering, artifact integrity, bounded snapshot scope, and future-row isolation. The final full suite passed 268 tests; Ruff and `git diff --check` also passed. The confirmation reader verifies the local and remote `main` SHA against the freeze and constructs only the matching Phase 11 cache path with the exact 2025 H2 filter; its fixture confirms rows outside that range are omitted.

## Frozen diagnostic procedure

This phase uses the Phase 11 signal solely to explain its failure: matched KOSPI or KOSDAQ index 20-session return `<= -4%`, signal after the completed session, next-session open entry, and close of entry-session 3 exit. Per-symbol executions are non-overlapping. Forward outcomes are used only as outcomes; the state at signal time uses completed information through that date. Signals without a complete exit inside the touched window are censored, not extended into Confirmation.

Daily market-state rows include index 1/3/5/10/20/40-session returns; 5/10/20-session realized volatility; rolling 20/60-session drawdown; distance from and sessions since 20/60-session highs; 5/20-session range; and current-cohort turnover and volume. Breadth and cross-sectional dispersion use the frozen cohort only. Turnover/volume aggregates therefore describe this cohort, not total exchange turnover.

Stress signal dates are grouped per market and per touched period. Dates merge when there are at most two intervening non-signal market sessions; episodes reset at the Development/Validation boundary. Overlapping KOSPI/KOSDAQ episodes are additionally assigned one joint macro stress cluster for uncertainty resampling. This rule is fixed independently of forward return. Episode outputs include start/end, duration, signal dates, maximum drawdown, minimum 20-session return, maximum volatility, minimum breadth, maximum dispersion, and a 20-session recovery path (flagged when censored by the touched-period end).

Descriptive depth states use episode minimum 20-session return: MILD `(-8%, -4%]`, MODERATE `(-12%, -8%]`, DEEP `(-20%, -12%]`, EXTREME `<= -20%`. Speed is FAST_SHOCK when the negative 5-session return contributes at least 50% of the absolute 20-session decline; SLOW_GRIND when that contribution is at most 25% and the signal is at least 10 sessions from its 20-session high; other observations are MEDIUM_DECLINE. Volatility, dispersion, and cohort-turnover LOW/MID/HIGH terciles are cut on breadth-quality-passing Development observations and held fixed for Validation. These are descriptive categories, not searched thresholds.

Rolling beta uses up to 60 completed paired stock/index daily returns ending at the signal date, requires at least 40 pairs, and has fixed descriptive states: LOW `<=0.75`, MID `(0.75,1.25]`, HIGH `>1.25`. The residual is `stock 3-session return − trailing beta × matched-index 3-session return`; it is a simple diagnostic, not a formal factor model.

## Phase 11-style return decomposition

Equal-weight means over the original per-symbol non-overlap executions are shown below. The matched-index return repeats a shared market observation across stock executions, so uncertainty is clustered by joint stress episode rather than treating stock rows as independent.

| Period | Raw events | Non-overlap events | Raw signal dates | Non-overlap date clusters | Market-specific signal dates | Joint macro clusters | Stock gross | Matched index | Stock minus index | Beta residual | Net 1× / 1.5× / 2× |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Development | 4,624 | 1,927 | 76 | 35 | 43 | 10 | +1.046% | +0.893% | +0.153% | +0.319% | +0.516% / +0.251% / −0.014% |
| Touched Validation | 5,239 | 2,114 | 83 | 41 | 51 | 7 | +0.479% | +0.366% | +0.113% | +0.162% | −0.051% / −0.316% / −0.581% |

The matched market component fell by 0.527 percentage points, 92.9% of the 0.567-point gross decline. Development's matched-index mean was 85.4% of the stock gross mean. The simple stock-minus-index excess fell by 0.040 percentage points; both excess means have 90% episode-bootstrap bands crossing zero. Beta residual means were below 0.53% in both periods, and the Validation beta-residual band also crosses zero. The data support a broad index-rebound explanation, not robust stock-level alpha after costs.

### KOSPI and KOSDAQ diagnostics

| Market / period | Non-overlap events | Stock gross | Matched index | Excess | Beta residual | Net at 1× |
|---|---:|---:|---:|---:|---:|---:|
| KOSPI Development | 888 | +1.423% | +1.228% | +0.195% | +0.438% | +0.893% |
| KOSPI Validation | 750 | +0.997% | +0.953% | +0.044% | +0.240% | +0.467% |
| KOSDAQ Development | 1,039 | +0.724% | +0.607% | +0.117% | +0.217% | +0.194% |
| KOSDAQ Validation | 1,364 | +0.194% | +0.044% | +0.150% | +0.118% | −0.336% |

This split is diagnostic only. In particular, Validation KOSPI's +0.997% gross is almost matched by its +0.953% index return and is not justification to retroactively narrow the rejected candidate to KOSPI.

## Episode and chronological anatomy

There were 15 market-specific Development episodes (KOSPI 7, KOSDAQ 8) and 12 Validation episodes (KOSPI 5, KOSDAQ 7), corresponding to 10 and 7 joint macro clusters. Validation therefore fails the eight-independent-cluster minimum by itself. Development had 9 MILD / 5 MODERATE / 1 DEEP episodes; Validation had 6 / 3 / 3; neither had an EXTREME episode. Signal-date mean 20-session index returns were nearly the same: −7.323% vs. −7.228% (SMD 0.035). Episode minimum 20-session returns averaged −7.112% vs. −9.281%, a deeper but small-sample Validation tail.

| Block | Stress episodes | Non-overlap events | Unique dates | Mean stress 20d | Stock 3d | Index 3d | Excess | Mean 20d vol | Mean 5d negative breadth | Mean 20d dispersion |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 H1 | 4 | 350 | 14 | −7.123% | +1.900% | +1.543% | +0.357% | 1.219% | 0.650 | 12.095% |
| 2023 H2 | 6 | 936 | 35 | −8.282% | +0.359% | +0.330% | +0.029% | 1.509% | 0.714 | 9.215% |
| 2024 H1 | 5 | 641 | 27 | −5.961% | +1.583% | +1.361% | +0.222% | 1.108% | 0.511 | 10.908% |
| 2024 H2 | 10 | 1,544 | 57 | −7.366% | +0.410% | +0.404% | +0.007% | 1.876% | 0.622 | 12.697% |
| 2025 H1 | 2 | 570 | 26 | −6.902% | +0.665% | +0.265% | +0.400% | 1.818% | 0.488 | 13.130% |

The path is alternating strong/weak half-years, not gradual decay: excess was weak in 2023 H2 and 2024 H2, then recovered in 2025 H1. That final touched block contains only two market-specific episodes, so it does not establish a stable new rule.

Episode outcomes are labelled SUCCESSFUL when the episode's mean non-overlap stock gross return is above zero and FAILED at or below zero; no outcome cutoff was fit. Development had 13 successful and 2 failed episodes; Validation had 6 of each. Development failed episodes averaged +0.182% matched-index return and −0.535% stock excess, while successful episodes averaged +1.379% index and +0.367% excess. Validation's successful episodes averaged +2.325% index and +0.104% excess; failed episodes averaged −0.550% index and +0.045% excess. These counts are too small to support a classifier or additional candidate.

### Depth, speed, volatility, breadth, and dispersion

| Diagnostic | Development | Validation | Interpretation |
|---|---:|---:|---|
| Episode FAST_SHOCK share | 3/15 (20.0%) | 11/12 (91.7%) | Speed composition shifted sharply, but returns do not support a stable fast-shock premium. |
| FAST_SHOCK stock gross / excess | −0.040% / −0.220% | +1.529% / +0.224% | Direction changed across periods; speed alone does not explain repeatable behavior. |
| MEDIUM_DECLINE stock gross / excess | +1.334% / +0.202% | −0.254% / +0.041% | Also decayed and changed direction. |
| Mean signal-date 20d realized volatility | 1.327% | 1.859% | SMD +0.909, the largest core feature shift. |
| Mean 5d negative-stock breadth | 0.636 | 0.582 | SMD −0.211; the mean breadth became somewhat less negative. |
| Mean 20d cross-sectional dispersion | 10.267% | 12.827% | SMD +0.662; Validation was more dispersed. |

Volatility tercile cutoffs from Development were 0.946% and 1.155%. HIGH-volatility events averaged +1.370% gross / +0.156% excess in Development and +0.570% / +0.182% in Validation; neither state clears the market-adjusted after-cost gate. LOW volatility in Validation has only two market signal dates. Dispersion tercile cutoffs were 9.369% and 12.726%. Dispersion-state returns were inconsistent: MID averaged +1.573% / −0.029% gross/excess in Development but −0.681% / −0.010% in Validation; LOW and HIGH states also failed to give a single sufficiently sampled, after-cost direction in both periods.

The broad/narrow breadth diagnostic uses 70% of observed cohort names with negative 5-session return as the broad-selloff boundary. BROAD_SELLOFF produced +0.573% gross / +0.261% excess in Development and +1.994% / +0.476% in Validation; however, Development beta residual was +0.292% (below 0.53% cost), Validation had only 7 macro clusters, and Development had only 19 unique dates. NARROW_SELLOFF produced +1.489% / +0.052% in Development and −0.675% / −0.164% in Validation. This is an interesting descriptive difference, but it fails the frozen mechanism gate and is not a candidate.

| Feature at market-signal dates | Dev mean / median / P10 / P25 / P75 / P90 | Val mean / median / P10 / P25 / P75 / P90 | SMD |
|---|---|---|---:|
| Index 20d return | −7.323 / −6.850 / −11.197 / −8.484 / −4.991 / −4.335% | −7.228 / −6.653 / −10.774 / −8.533 / −5.193 / −4.514% | +0.035 |
| Index 5d return | −2.062 / −2.578 / −4.799 / −3.950 / −0.528 / +1.504% | −1.604 / −2.240 / −7.223 / −4.128 / +1.051 / +4.237% | +0.122 |
| 20d realized volatility | 1.327 / 1.221 / 0.931 / 1.061 / 1.371 / 1.928% | 1.859 / 1.607 / 1.144 / 1.302 / 2.324 / 3.222% | +0.909 |
| 5d negative breadth | 0.636 / 0.714 / 0.284 / 0.476 / 0.811 / 0.893 | 0.582 / 0.680 / 0.140 / 0.341 / 0.800 / 0.907 | −0.211 |
| 20d dispersion | 10.267 / 9.792 / 7.321 / 8.431 / 12.160 / 13.329% | 12.827 / 11.895 / 8.167 / 9.317 / 14.719 / 20.288% | +0.662 |
| Cohort turnover ratio vs prior 20d median | 0.890 / 0.774 / 0.442 / 0.595 / 0.977 / 1.208 | 1.165 / 0.936 / 0.550 / 0.771 / 1.504 / 1.877 | +0.443 |
| Rolling 20d drawdown | −7.953 / −7.683 / −11.061 / −9.359 / −6.307 / −5.010% | −7.811 / −7.431 / −11.704 / −9.625 / −5.955 / −4.659% | +0.054 |
| Stress duration (sessions) | 14.077 / 11 / 4 / 9 / 21.5 / 26 | 18.614 / 20 / 7 / 11 / 21 / 33 | +0.489 |
| Rolling beta (60d) | 0.765 / 0.702 / 0.157 / 0.386 / 1.082 / 1.439 | 0.827 / 0.796 / 0.234 / 0.507 / 1.142 / 1.448 | +0.120 |
| Market-adjusted 3d return | 0.153 / −0.142 / −4.782 / −2.082 / 2.134 / 4.808% | 0.113 / −0.360 / −4.966 / −2.573 / 2.171 / 4.970% | −0.008 |

Prices, volume, and cohort turnover permit recent-return, volatility, and liquidity diagnostics. Historical valuation data with safe point-in-time semantics are not available, so “cheap” versus “expensive” was not tested. No factor or strategy was selected from these descriptive splits.

Stock-level recent-return and liquidity/volatility terciles are defined on Development and held fixed for Validation. The lowest stock 20d-return tercile (recent losers) averaged +1.701% gross / +0.924% beta residual in Development, falling to +0.657% / +0.140% in Validation. The highest (recent winners) averaged +0.632% / −0.116% and +0.343% / +0.181%. Low/high stock-turnover terciles produced gross +1.163%/+0.824% in Development and +0.497%/+0.469% in Validation; there was no stable after-cost distinction. Low/high stock-volatility terciles produced +0.897%/+1.031% gross in Development and +0.042%/+0.825% in Validation, also without a stable net mechanism. Valuation state is `NOT_AVAILABLE_POINT_IN_TIME_SAFE_DATA`.

## Beta, contribution concentration, and uncertainty

The fixed beta groups are based on trailing pre-signal estimates. LOW_BETA had 1,033 Development and 957 Validation events, with gross +0.725% / +0.196% and beta residual +0.364% / +0.085%. MID_BETA had 562 / 731 events, gross +1.471% / +0.978%, residual +0.573% / +0.255%. HIGH_BETA had only 324 / 406 events, gross +1.300% / +0.375%, residual −0.267% / +0.175%. Thus Development did not depend mainly on high-beta names; its high-beta residual was negative, and all groups weakened or remained below the 0.53% residual cost scale in Validation.

| Period | Executed market episodes | Top 1 share of positive trade contribution | Top 3 | Top 5 |
|---|---:|---:|---:|---:|
| Development | 15 | 9.64% | 31.86% | 44.64% |
| Validation | 12 | 12.50% | 25.78% | 37.51% |

Neither period's positive trade contribution was dominated by one or five episodes under this measure. The top five's gross point contribution can exceed the net total because negative episodes offset it; those figures are not shares of total net return.

Episode-cluster bootstrap bands use 2,000 resamples, a fixed seed, and preserve all stock events within each sampled joint KOSPI/KOSDAQ stress cluster. No individual stock-row bootstrap or p-values were used.

| Period | Joint clusters | Gross 90% band (median) | Index excess 90% band (median) | Beta residual 90% band (median) |
|---|---:|---|---|---|
| Development | 10 | [0.426%, 2.110%] (1.098%) | [−0.041%, 0.488%] (0.153%) | [0.051%, 0.795%] (0.333%) |
| Validation | 7 | [0.114%, 1.290%] (0.481%) | [−0.194%, 0.280%] (0.118%) | [−0.161%, 0.408%] (0.163%) |

The intervals are descriptive and wide; Validation has only seven joint clusters. In particular, the index-excess bands include zero in both periods and the Validation beta-residual band includes zero.

## Ranked explanation and mechanism decision

1. **MARKET_BETA_EFFECT — STRONG.** Matched-index return was +0.893% of +1.046% Development gross. Its 0.527-point decline accounts for 92.9% of the total 0.567-point gross decay. Mean stock-minus-index excess was just +0.153% / +0.113%.
2. **STRESS_COMPOSITION_SHIFT — STRONG, descriptive.** Validation signal-date 20d volatility rose by 0.531 points (SMD +0.909), dispersion by 2.559 points (SMD +0.662), turnover ratio by 0.275 (SMD +0.443), and mean episode minimum 20d return deepened. These regime shifts co-occurred with decay, but the coarse regimes do not establish a stable causal filter.
3. **STRESS_DEPTH_SHIFT — WEAK.** The trigger-date 20d return distribution barely shifted (SMD +0.035). Validation had three DEEP episodes versus one in Development, but counts are small and both had no EXTREME episode.
4. **STRESS_SPEED_SHIFT — measured shift, weak explanatory support.** FAST_SHOCK went from 3/15 to 11/12 episodes, yet FAST_SHOCK outcomes changed from −0.040% gross / −0.220% excess to +1.529% / +0.224%. The direction does not replicate.
5. **BREADTH_SHIFT — MODERATE.** Mean negative 5d breadth declined 0.055 (SMD −0.211); broad/narrow outcomes differ, but no one-feature broad-selloff rule satisfies both-period episode, date, and residual-cost gates.
6. **DISPERSION_SHIFT — STRONG shift, unsupported mechanism.** Dispersion was higher in Validation (SMD +0.662), but the return ordering across LOW/MID/HIGH states was not stable.
7. **EPISODE_CONCENTRATION — WEAK as an explanation.** Top-one episode shares were 9.64% and 12.50% of positive contribution; top-five were 44.64% and 37.51%.
8. **CURRENT-LISTING SURVIVORSHIP / MISSING FLOW — MODERATE limitation, not attributed as the decay cause.** The cohort lacks delisted listings and point-in-time membership; investor flow is unavailable. These limit both estimates but do not explain the observed period difference with available evidence.

The Phase 11 effect is not a smooth fade: two weaker half-years alternate with stronger ones, and 2025 H1 has a larger stock-minus-index mean than the earlier blocks despite the lower gross than Development. Still, a reproducible new mechanism must pass all preregisterable gates; none did. The broad-capitulation case is suggestive but fails touched Development residual-cost/date criteria and Validation's eight-cluster minimum. Other single-feature mechanisms fail period consistency, minimum independent samples, or after-cost market-adjusted scale. Combining many filters or selecting a different market/horizon would be retuning and was not done.

## Cost audit

The Phase 11/12 research assumption remains unchanged: fee 0.015% each side, sell tax 0.20%, and slippage 0.15% each side; round trip `2 × 0.015 + 0.20 + 2 × 0.15 = 0.53%`. The fee is `ASSUMED` because no account/channel was pinned. KIS's public schedule lists KOSPI sale tax as 0.05% plus 0.15% special rural tax and KOSDAQ sale tax as 0.20%; the component is `VERIFIED_CURRENT` from the [KIS fee and tax schedule](https://securities.koreainvestment.com/main/customer/guide/_static/TF04ae010000.jsp?tab=3). Slippage is `CONSERVATIVE` as a fixed haircut but unverified against historical executions and not a guaranteed bound in stress/illiquid names. No cost assumption or historical Phase 11 result was changed.

## Preregistration, confirmation, and artifact disposition

No Phase 12 candidate exists; therefore the preregistration status is `NOT_CREATED`, its candidate rule and freeze SHA are null, and the Confirmation authorization flag is false. `phase12-confirmation.json` records `NOT_RUN` without opening 2025 H2. The one-shot Confirmation pass gate was not applied because its prerequisite—one genuinely new, frozen mechanism—did not exist. External 2026 remains `NOT_READ`; protected Holdout remains `NOT_READ`. No Shadow is authorized.

Machine-readable outputs are under `runtime/research/phase12/` and include the bounded dataset manifest, compressed market-state and event rows, episode records and recovery paths, breadth/volatility/dispersion/liquidity/stock-characteristic analyses, chronological blocks, stock/index/beta decomposition, distribution shifts, episode concentration, bootstrap, cost audit, ranked root cause, hypothesis gate, non-created preregistration, Confirmation guard record, previous-phase snapshot, artifact index, and integrity report. Runtime artifacts are ignored by Git under the repository's runtime policy; source, tests, and this report are committed. Artifact integrity checked 23 indexed artifacts and passed. The prior Phase 5–11 manifest/index snapshot remains 23/23 unchanged (no added, removed, or changed files).

## Explicit answers

| Question | Answer |
|---|---|
| Q1. Why did Phase 11 Development outperform Validation? | The matched-index component declined by 0.527 points and explains 92.9% of the 0.567-point gross decay; volatility, dispersion, and episode composition also shifted. |
| Q2. How much was explained by the matched index? | Development matched-index return averaged +0.893%, 85.4% of +1.046% stock gross; the market-component change explains 92.9% of gross decay. |
| Q3. Did meaningful stock excess remain? | Arithmetic excess was +0.153% Development and +0.113% Validation. Episode-bootstrap bands include zero; beta residuals are below 0.53% cost, so no robust cost-sized stock edge remains. |
| Q4. Did stress depth differ? | Trigger-date 20d returns barely differed (−7.323% vs. −7.228%, SMD +0.035). Validation had more DEEP episodes (3 vs. 1), with low counts. |
| Q5. Did stress speed differ? | Yes: FAST_SHOCK share rose from 20.0% to 91.7%. But FAST_SHOCK returns were negative in Development and positive in Validation, so speed is not a stable explanation. |
| Q6. Did volatility differ? | Yes. Mean signal-date 20d volatility rose 1.327% to 1.859% (SMD +0.909). State-conditioned returns do not support a stable new rule. |
| Q7. Did breadth differ? | Mean fraction with negative 5d return fell 0.636 to 0.582 (SMD −0.211). Broad/narrow outcomes changed sharply, but the mechanism gate failed. |
| Q8. Did dispersion differ? | Yes. Mean cross-sectional dispersion rose 10.267% to 12.827% (SMD +0.662); state returns are inconsistent between periods. |
| Q9. Was Development concentrated in a few episodes? | No under positive-contribution share: top 1 was 9.64%, top 3 31.86%, top 5 44.64%. |
| Q10. Was a stable mechanism visible in both touched periods? | No. Market beta explains the broad return, while no simple, after-cost, adequately sampled mechanism passes both-period gates. |
| Q11. Did a new candidate emerge without Phase 11 retuning? | No: `MARKET_STRESS_MECHANISM=NOT_SUPPORTED`; `PHASE12_CANDIDATE=NOT_CREATED`. |
| Q12. What is the exact new candidate rule? | None. Phase 11's rejected `20d index return <= -4%`, next-open/third-session-close rule remains historical and rejected. |
| Q13. Did untouched 2025 H2 Confirmation pass? | Not evaluated (`NOT_RUN`); no candidate qualified for freeze. |
| Q14. Should 2026 external evidence be opened? | No. Keep `EXTERNAL_2026=NOT_READ` for a later, separately authorized phase. |
| Q15. Is anything ready for prospective Shadow? | No: `SHADOW_NEXT_SESSION=NO`; `ALPHA=UNPROVEN`; `LIVE=DISABLED`. |

Exact next step: retain the negative mechanism verdict and keep Confirmation, 2026 external data, and protected Holdout closed. A future study must define a genuinely distinct mechanism on a new development protocol before any untouched data is considered.

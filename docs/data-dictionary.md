# Data dictionary

All times are UTC unless a field says otherwise. Money never appears in public files: only % of
balance and R multiples. Accounts appear only as a 16-hex-character salted SHA-256 hash.

## `data/latest.json` (schema `zumi.latest`, version `v: 1`)

Pretty-printed, keys sorted, pure ASCII. Rewritten only when its content changes (the `day` field
changes once per UTC day, which doubles as a heartbeat).

| field | type | meaning |
|---|---|---|
| `schema`, `v` | str, int | `"zumi.latest"`, `1`. Breaking changes bump `v`. |
| `day` | `YYYY-MM-DD` | UTC date of the build. The as-of cut-off. |
| `generated_utc`, `generated_unix` | str, int | When this content was written. The EA treats the file as stale after `InpGhMaxAgeHours` (default 36 h). |
| `asof` | `"D-1"` | Causality rule: every value is the last observation dated **strictly before** `day`. |
| `stale_after_h` | number | Suggested staleness limit for consumers. |
| `series.<ID>.d` | date | Observation date of the value used. |
| `series.<ID>.v` | number | Value (percent for yields/breakevens, index level otherwise). |
| `series.<ID>.p` | number | Previous observation. |
| `series.<ID>.c1` / `c1p` | number | `v - p` and percent change vs `p`. |
| `series.<ID>.c5` | number | `v` minus the value 5 observations back. |
| `series.<ID>.age` | int | Days between `d` and `day` (1 = yesterday). |
| `events.next[]` | list | Next high-impact US releases: `ev` (NFP/CPI/FOMC), `utc`, `t` (unix seconds), `unc` (1 = date not confirmed). |
| `events.recent[]` | list | Releases in the last 24 h (same fields). |
| `events.covered_until` | date | Last date in the calendar file. Extend it before this date passes. |
| `flags.vix_high`, `flags.vix_jump` | 0/1 | VIX >= `alerts.vix_level`; last VIX change >= `vix_jump_pct` % or `vix_jump_abs` points. |
| `gold` (optional) | object | stooq XAUUSD D-1: `d`, `c` close, `c1`, `c1p`, `rng`, `atr14` (ATR known before `d`), `move_atr` = `c1 / atr14`, `age`. **Disabled by default** (`gold.enabled: false`): stooq returns a non-CSV page to GitHub runners. |

FRED series (public CSV endpoint `fredgraph.csv?id=...`, no key):

| ID | meaning | publication lag |
|---|---|---|
| `DGS10`, `DGS2` | US Treasury 10y / 2y nominal yield, % | H.15, posted next business day ~16:15 ET |
| `DFII10` | 10y TIPS real yield, % | same |
| `T10YIE` | 10y breakeven inflation, % | same day evening / next day |
| `DTWEXBGS` | Broad trade-weighted USD index | H.10, **weekly** (Monday): `age` up to ~10 days is normal |
| `VIXCLS` | CBOE VIX close | next day |
| `DCOILWTICO` | WTI crude, USD/bbl | a few days |
| `SP500` | S&P 500 close | next day |

Because of these lags the newest value on day D is usually from D-1 or D-2 (the research note
calls this "L2" availability). Use `age`; never assume `d` is yesterday.

## `data/quality.json`

`{"ok": bool, "issues": [{"sid", "kind", "level", "msg"}], "since_utc"}`. Written only when the set
of issues changes.

| kind | level | effect |
|---|---|---|
| `range` | error | value outside `quality.range` -> latest.json and that series' history are **not** updated |
| `spike` | error | 1-observation change above `quality.max_abs_c1` / `max_abs_c1p` (data-error sized, not market-move sized) |
| `schema`, `coverage` | error | malformed output / fewer than `min_series_ok` usable series |
| `stale` | warn | observation older than `quality.max_age_days` |
| `fetch`, `missing` | warn | download failed (history kept) / no observation before `day` |

`alerts.yml` comments on the alerts PR for every `error` and every `stale` warning.

## `data/history/<ID>.csv`

`date,value` (FRED) or `date,open,high,low,close` (`XAUUSD.csv`, stooq). Each run re-fetches the
last `refetch_days` (60) days and merges them (new values win, which captures revisions).

## `data/calendar/events_us.csv`

```
date,event,time_et,flag
2026-12-04,NFP,08:30,uncertain
```

* `date`, `time_et`: release date and time in America/New_York (DST is handled in code).
* `event`: `NFP`, `CPI`, `FOMC` (other codes pass through).
* `flag`: empty = checked against the official schedule; `uncertain` = not yet confirmed.

There is no reliable free calendar API, so this file is maintained by hand:

1. Each December (and whenever BLS or the Fed publishes or changes a schedule) add next year's rows
   from the official pages: BLS release calendar (Employment Situation = NFP, CPI) and the Federal
   Reserve FOMC meeting calendar (statement time 14:00 ET on the last meeting day).
2. Mark a row `uncertain` until it is confirmed, then clear the flag.
3. Keep the file sorted by date. Lines starting with `#` are comments.
4. `quality.json` warns when no upcoming event is left (`events.covered_until` passed).

The initial file came from a research pass and is **knowledge-based, not downloaded**: 27 rows
(2025 shutdown period and 2026) are flagged `uncertain` and should be verified.

## EA reports: two modes

The repository is **public**, so `config/settings.json` has `reports.public_mode: true`:

| mode | EA input | schema | accepted here |
|---|---|---|---|
| aggregate (default) | `InpGhReportMode=0` | v2, `kind: "ea_agg"` | yes |
| full detail | `InpGhReportMode=1` | v1, `kind: "ea_report"` | **rejected** (only for a PRIVATE repo with `public_mode: false`) |

Both are sent as `repository_dispatch` with `event_type: "ea_report"`. Note that GitHub shows the event payload of a
workflow run to anyone who can read the repository, so per-trade data must never be *sent* to a public repo; the
server-side rejection only keeps it out of the files.

### Aggregate report (v2, `kind: "ea_agg"`): the only format for this public repo

No individual trades: no times, prices, SL/TP, tickets, comments or money amounts. Top level (8 keys, GitHub allows at
most 10):

| key | meaning |
|---|---|
| `v`, `kind` | `2`, `"ea_agg"` |
| `acct` | 16 hex chars: SHA-256 of `salt:login:server`, computed in the EA (a clear login is never sent) |
| `rid` | `<acct>-<ts>` |
| `part`, `parts` | always `1`, `1` (an aggregate report is small) |
| `ts` | unix UTC, **rounded down to the report slot** (`InpGhReportEveryMin`, default 60 min; 15 min for flag and day-end reports) |
| `data` | see below |

`data`:

| key | meaning |
|---|---|
| `ea`, `ver`, `reason` | EA name, bridge version, `periodic` / `dayend` / `flag` / `manual` / `deinit` / `heartbeat` (bridge 2.4: once per slot with no closed trade, carries the market context); any other reason is stored as `other` |
| `day` | server date of the report (`YYYY-MM-DD`; Exness servers run on UTC) |
| `phase` | current EA phase name |
| `eq` | `day_pl_pct` (closed P/L today), `eq_day_pct` (equity vs day start), `float_pct` (open P/L), all % of balance |
| `mkt` | `sym`, `move_atr` (today's move vs previous D1 close in ATR14(D1) units), optional `ctx` (bridge 2.4, below). No prices. |
| `periods.day` / `.week` / `.mtd` / `.r30` | aggregate block for today (server day), the rolling last 7 days, month to date, the rolling last 30 days (coordinator input) |
| `flags[]` | `{key, msg}` raised by the EA with `ZGB_Flag()` (published: keep prices out of `msg`) |
| `shadow` | optional (bridge 2.5 / EA161 v1.32): blocked-signal shadow statistics, below |

`mkt.ctx`: EA market context (EA161 v1.31 / bridge 2.4), rebuilt on each new M15 bar from the EA's analyst state.
Only bucketed or relative values: **no prices, no times, no method names**. `scripts/agg.py` (`sanitize_ctx`) checks every
key, range and code; anything else is dropped and only its key name is logged. Typical size 0.8-1.2 KB.

| key | meaning |
|---|---|
| `tf.D1` / `.H4` / `.H1` / `.M15` | last CLOSED bar: `dir` EMA50 slope over 3 bars (-1/0/1), `rsi` RSI14 (0..100), `adx` ADX14 (0..100), `pb` Bollinger %B x 100 (-100..200), `e50` / `e200` close vs EMA50 / EMA200 (-1/1), `ichi` close vs Ichimoku 9/26/52 cloud (-1 below, 0 in, 1 above) |
| `phase` | EA phase label `D1 band + H4 state + vol tercile` without separators (`[A-Z]{2,8}`, e.g. `SDPBL` = D1 SD, H4 PB, vol L) |
| `ref` | reference side of the analyst (-1 SELL / 1 BUY): scenarios a/b/c and the `REF:` flags are relative to it |
| `rev[]` | at most 8 codes `<TF>:<code>`, TF `W1 D1 H4 H1 M15`, code `SWEEP_HI` `SWEEP_LO` (D1: prior-day high/low taken and closed back inside; H1: 20-bar high/low), `PIN_BULL` `PIN_BEAR` `ENGULF_BULL` `ENGULF_BEAR` (closed candle), `DIV_BULL` `DIV_BEAR` (RSI divergence on the last two confirmed fractals, H4/H1); or `REF:R1`..`REF:R9` = EA reversal factors against `ref` (R1 RSI extreme, R2 stochastic extreme, R3 RSI divergence, R4 Bollinger re-entry, R5 stretched run, R6 20-bar sweep, R7 H1 CHoCH, R8 H4 CHoCH, R9 D1/H4 reversal candle). Order: sweeps, candles, divergences, then REF flags (R7, R8, R9 first) |
| `sr_up_atr`, `sr_dn_atr` | distance to the nearest level above / below (D1 and W1 high/low, D1/H4 fractals, round 50) in ATR(H1), 0..20 |
| `vol` | `atrp` ATR(H1) in % of price, `terc` H1 ATR percentile tercile `L` / `N` / `H`, `rk` that percentile x 100 |
| `mv` | `h4`, `h24`: change of the H1 close over the last 4 / 24 hours in ATR(H1) units (to grade earlier forecasts) |
| `w1`, `d1`, `h4` | last closed candle type: `OTHER ENG_UP ENG_DN PIN_UP PIN_DN INSIDE OUTSIDE DOJI MARU_UP MARU_DN` |
| `an` | K-NN analogs: `pl` / `ps` share of resolved analogs whose long / short 1-ATR target came first, `n` resolved analogs |
| `scn` | 3-scenario forecast of the K-NN analogs: `n` analogs (100), `h` horizon (24 h), `a` continuation, `b` pullback then resume, `c` reversal, each `p` (%, int) and signed distances from the price at the report in ATR(H1): `to_atr` median 24 h target, `b.pb_atr` pullback level, `c.ext_atr` extreme against `ref`; `none` % of analogs that reached neither side. Distances are limited to +-50 ATR |
| `dir_auc` | analog direction AUC (Mann-Whitney of the live `pl` vs the realised first-touch side) over the cases of the last 30 days with a resolved label; ~0.5 = no direction skill |
| `cr` | crisis layer: `OFF`, `OK` or `BLOCK:<VOL SPREAD GAP NEWS DAYSTOP WEEKSTOP SAMESIDE OTHER>` |
| `cds` | EA161 v1.33: Confluence Direction Score x 100, int -100..100 (weighted vote of EMA stack, Ichimoku, RSI, MACD, ADX/DI, HH/HL structure, BOS/CHoCH, trendline, Fibonacci 0.5-0.618 on W1..M5 + reversal candle at S/R and RSI divergence on H1/H4; 0 within +-30 min of a listed USD event). Out of sample it has no reliable directional edge (AUC ~0.51): a summary, not a signal |
| `side` | CDS verdict: `buy` (cds >= InpCdsTheta x 100), `sell`, `flat` |
| `gside` | `global.side` the EA applies now: `both`, `buy`, `sell`, `auto` (NEUTRAL control = `both`) |
| `cds_tf` | per-TF sub-score x 100 (int -100..100) for `W1 D1 H4 H1 M15 M5` |

The daily page renders the last context of the day and a scenario log of every report of the day. A `heartbeat` that is
identical to the previous stored report of that account and day (apart from `ts`) is not stored, so it creates no commit.

`shadow`: blocked signals traded virtually (EA161 v1.32 / bridge 2.5, `ZGB_SetShadowStats`). Every signal that reached
the EA's order entry and was **not** sent gets one reason and a virtual trade with the SL of the order and the TP the order
would have had (EA135: 1R with `InpEA135ExitMode=1`), resolved on closed M1 bars: first touch of SL or TP (both in one bar =
SL), 24 h time-out (and the Friday flat) = the R at that bar's close. Counted on the server day the virtual trade closed, like
real trades. Counts and R only: **no prices, no times, no method names**. Size <= 8 KB.

| key | meaning |
|---|---|
| `v`, `open` | schema `1`; virtual trades still open |
| `periods.day` / `.week` / `.mtd` / `.r30` | `{REASON: {n, wins, losses, to, net_r}}` (same windows as the aggregate blocks; `to` = time-outs; win = R > 0, loss = R < 0) |
| `by_method` | rolling 30 days, top 30 cells by `n`: `{"M###:REASON": {n, wins, net_r}}` (method code from the entry comment, the same code as real trades' `by_method`) |
| `by_phase` | rolling 30 days, top 40: `{"PHASE:REASON": {n, wins, net_r}}`, PHASE as `mkt.ctx.phase` (`[A-Z]{2,8}`) or `none` |
| `by_session` | optional (bridge 2.7 label, sent by an EA that uses `ZGB_SessionOf`): rolling 30 days, at most 36 cells `{"SESS:REASON": {n, wins, net_r}}`, SESS `ASIA` / `LON` / `NY` / `OFF` of the blocked signal's time (UTC) |

Reasons (first block wins, in the EA's order): `CAP` base-slot pre-check (MaxOpen, same entry, NO-STACK); then at the order
entry `CELL` (phase cell mask off, S4 direction / pullback), `NEWS` (news-day veto, crisis NewsFlat window), `CAP` (max per
method, max open = open-risk cap, one entry per minute and side, EA131 aggregate risk / margin), `BREAKER` (bad-month
breaker), `CTRL` (control channel: pause, disabled, `risk_mult` 0, extra news block), `SIDE` (v1.33 direction bias: `global.side` buy / sell / auto against the CDS, or the local `InpCdsGate`), `SPREAD` (crisis spread spike, EA131
spread limit, EA136 spread-to-risk), `CRISIS` (volatility, gap, day/week stop, max same side), `OTHER` (weekend cut, EA134 /
EA136 / EA135, broker). A NEWS / CAP signal whose phase cell is off counts as `CELL`; signals of methods that are not in the
active selection are not recorded. Engine-local pre-checks inside modules, lanes and M1 engines are not counted.
Validator (`agg.sanitize_shadow`): reason vocabulary, `M\d{3}`, phase and session regexes, `wins + losses <= n`, `|net_r| <= 2.5 x n`,
list caps; anything else is dropped (key names only are logged, cell keys as `<invalid>`). The daily / weekly / monthly pages
render "Blocked signals (shadow)" for `day` / `week` / `mtd`: per reason blocked n, shadow win% and R next to the real trades
of the same period. Tests: `tests/test_shadow.py`.

Aggregate block (positions that are fully closed, counted at their last exit):

| key | meaning |
|---|---|
| `from`, `to` | period dates |
| `n`, `wins`, `losses`, `be` | closed positions and outcome by sign of the net result |
| `win_pct` | `wins / (wins + losses)` |
| `net_r`, `avg_r`, `gross_win_r`, `gross_loss_r`, `pf` | R from the SL at entry (volume-weighted over partial closes); PF = gross win R / gross loss R |
| `n_r` | positions that had an SL (R defined) |
| `net_pct` | sum of the positions' net results, % of balance |
| `max_ls`, `cur_ls`, `lead_ls` | longest losing streak, losing streak at the end, losing streak at the start (lets periods be chained) |
| `dd_pct` | max peak-to-trough of the cumulative closed P/L % within the period |
| `rule` | counts: `no_sl`, `risk_cap` (risk > `InpGhRiskCapPct`), `news` (entry within +-`InpGhNewsBlockMin` of NFP/CPI/FOMC), `daily_cap` (days with closed P/L <= -`InpGhDailyCapPct`) |
| `by_method` | `{"M001": {n, wins, win_pct, net_r, cur_ls, l20_n, l20_wins}, ...}`: **method codes**, never names (see below); `cur_ls` = current losing streak of the group, `l20_*` = its last 20 trades |
| `by_phase` | same per phase name |
| `by_method_phase` | same per `"M###:PHASE"` (used by the coordinator rules) |
| `by_side` | bridge 2.7: keys `buy` / `sell` only (direction of the entry deal), same fields as `by_method` groups: coordinator rule R09 (summed over the stored `day` blocks of the last `side_window_days`). Any other key is dropped (logged as `<invalid>`). EAs compiled with bridge <= 2.6 do not send it (R09 then never fires) |
| `by_session` | bridge 2.7: keys `ASIA` / `LON` / `NY` / `OFF` only, same fields as `by_method` groups: session of the entry time by UTC hour band, ASIA 00-08, LON 08-12, NY 12-20, OFF 20-24 (`ZGB_SESS_*_H` in the bridge = `agg.SESSION_BANDS_UTC`). Every band is a union of `grid` buckets, so it reveals no entry time the month-to-date grid does not. An extra dimension: phase keys and control codes are unchanged; other keys are dropped |
| `grid` | `mtd` only: `{"Mon": {"08": {n, wins}}}` weekday x 4-hour UTC bucket of the entry; cells with `n < 3` carry the count only |
| `n_ex`, `mae_r`, `mfe_r` | optional average worst / best excursion in R (positions tracked while open) |

**Method codes.** The EA turns each method name (the entry comment) into `M001`..`M999`, derived from a salted hash and
recorded in the local file `MQL5\Files\zumi_method_codes.csv` (`code,name`). That file stays on the user's PC and is never
committed. `M000` = no comment / unknown. Bridge 2.5 / EA161 v1.32: the control channel looks codes up through the same
function (`ZGB_CodeForComment`, the entry's order comment), so `control.json` codes taken from `by_method` hit those entries.
Base-slot and module comments carry the side (`L` / `S`), so their codes are per side.

**Validator** (`scripts/agg.py`, whitelist): unknown keys are dropped wherever they appear and their key names (never the
values) are printed in the workflow log. Specifically it strips `trades`, `log`, prices, times, tickets, comments, absolute
money and account keys; folds any `by_method` key that is not `M###` into `M000`; drops invalid grid buckets; and removes
win counts from grid cells with fewer than 3 trades. Tests: `tests/test_agg.py`.

### Detail report (v1, `kind: "ea_report"`): PRIVATE repositories only

Accepted only when `reports.public_mode` is `false`. One item per closing deal in `data.trades[]`: `deal`, `pos`, `sym`,
`side`, `vol`, `vol_in`, `t_in`, `t_out`, `in`, `out`, `sl`, `tp` (at entry), `r`, `pl_pct`, `risk_pct`, `cmt`, `magic`,
`phase`, `reason`, `mae_r`, `mfe_r`; plus `log[]` (`{t, m}`), `flags[]` (`{key, msg, t}`), `eq` and a fuller `mkt`.
Split into parts when larger than `InpGhMaxPayloadBytes`.

## Stored report files

* `reports/YYYY/MM/DD/<acct>_<YYYYMMDDTHHMMSSZ>.json`: the sanitised aggregate report, pretty JSON. Re-sending an
  identical payload writes nothing.
* Private detail mode only: `reports/journal/<acct>/deals.jsonl` (deals upserted by ticket) and `positions.csv`
  (positions rebuilt from deals, partial closes volume-weighted).
* `reports/daily/YYYY-MM-DD.md`: the `day` block of the last report of that day, plus equity, market move and EA flags.
* `reports/weekly/YYYY-Www.md`: the `week` block (rolling 7 days) of the last report in that ISO week. Gold does not
  trade at weekends, so the last report of the week covers Monday to Friday. If no `week` block exists, the daily blocks
  are merged.
* `reports/monthly/YYYY-MM.md`: the `mtd` block of the last report in the month (if the EA was offline on the last
  trading days, the page stops at the last report).
* `reports/agents/YYYY-MM-DD.md` / `.json` (schema `zumi.agents_status` v1, `scripts/agents_status.py`, hourly in
  `data.yml`): one health row per agent and sub-agent of `docs/agents.md` (`agent`, `sub`, `status` OK / WARN / FAIL /
  SKIP = not checkable offline, `reason`, `metrics`), `overall`, `market_open`, and per account `kpis`: `trades_today`,
  `days_traded_7d` / `_30d`, `profitable_days_7d` / `_30d` (day blocks with `net_pct` > 0), `reported_days_*`,
  `weekdays_*`, `cur_losing_streak`, `dd_week_pct`, `dd_mtd_pct`. EA ages are open-market hours (gold closed Friday
  21:00 to Sunday 23:00 UTC). Rewritten only when a status, a reason or a KPI changes. Aggregate data only.
  `kpi.departments[]` / `kpi.company.<acct>[]` (requirement E5, `docs/agents.md` section 6): `id`, `dept`, `kpi`, `scope`
  (account hash or `repo`), `value`, `n` (sample), `window_d`, `op` / `target` / `warn` (pre-registered in
  `config/settings.json` > `agents_kpi`), `status` (OK at the target, WARN within `warn`, FAIL beyond, SKIP = not
  measurable from repo files, no data or `n < min_n`), `reason`, `trend.7d` / `trend.30d` = `{cur, prev, delta, dir
  up/down/flat/n/a, better}` (the last 7 / 30 days versus the 7 / 30 days before; |delta| <= `flat` = flat). Company
  block: net R 7 / 30 days, % weekdays with trades (from the first stored report on), % profitable days, MTD drawdown,
  win% after coordination, coordination lift = win% of the real trades minus win% of the `shadow` CTRL signals (pp).
  Repo KPIs: control refresh overdue > 48 h (`control/CHANGELOG.md`), data freshness replayed on `data/history`, summary
  pages present, `control.json` valid, stored reports that would not pass `agg.sanitize_v2` unchanged (privacy).
  Optional `agents_kpi.scenario_ledger` CSV (`date,hit`) feeds the analyst hit rate.
* `docs/dashboard/data.json` (`aggregates`): per account the daily series, the latest `week` and `mtd`, per-month `mtd`
  blocks, an all-time merge, and the `mtd` method/phase/grid tables.

Merging (`agg.merge_aggs`, used for fallbacks and the all-time total): counts, R, % and rule counts add up exactly;
streaks chain through `lead_ls` / `cur_ls`; the merged drawdown is a lower bound (the largest part drawdown or the
drawdown of the end-of-part P/L path).

## Control channel

`control/control.json` (schema v1), `control/CHANGELOG.md` and `control/reason_codes.md`: see
[`docs/control.md`](control.md). `reports/coord/YYYY-MM-DD.md` / `.json`: coordinator suggestions (rule hits, proposed
changes and a full proposed `control.json`).

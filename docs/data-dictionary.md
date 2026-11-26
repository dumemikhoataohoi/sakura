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
| `ea`, `ver`, `reason` | EA name, bridge version, `periodic` / `dayend` / `flag` / `manual` / `deinit` |
| `day` | server date of the report (`YYYY-MM-DD`; Exness servers run on UTC) |
| `phase` | current EA phase name |
| `eq` | `day_pl_pct` (closed P/L today), `eq_day_pct` (equity vs day start), `float_pct` (open P/L), all % of balance |
| `mkt` | `sym`, `move_atr` (today's move vs previous D1 close in ATR14(D1) units). No prices. |
| `periods.day` / `.week` / `.mtd` | aggregate block for today (server day), the rolling last 7 days, month to date |
| `flags[]` | `{key, msg}` raised by the EA with `ZGB_Flag()` (published: keep prices out of `msg`) |

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
| `by_method` | `{"M001": {n, wins, win_pct, net_r}, ...}`: **method codes**, never names (see below) |
| `by_phase` | same per phase name |
| `grid` | `mtd` only: `{"Mon": {"08": {n, wins}}}` weekday x 4-hour UTC bucket of the entry; cells with `n < 3` carry the count only |
| `n_ex`, `mae_r`, `mfe_r` | optional average worst / best excursion in R (positions tracked while open) |

**Method codes.** The EA turns each method name (the entry comment) into `M001`..`M999`, derived from a salted hash and
recorded in the local file `MQL5\Files\zumi_method_codes.csv` (`code,name`). That file stays on the user's PC and is never
committed. `M000` = no comment / unknown.

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
* `docs/dashboard/data.json` (`aggregates`): per account the daily series, the latest `week` and `mtd`, per-month `mtd`
  blocks, an all-time merge, and the `mtd` method/phase/grid tables.

Merging (`agg.merge_aggs`, used for fallbacks and the all-time total): counts, R, % and rule counts add up exactly;
streaks chain through `lead_ls` / `cur_ls`; the merged drawdown is a lower bound (the largest part drawdown or the
drawdown of the end-of-part P/L path).

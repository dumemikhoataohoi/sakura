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
| `gold` (optional) | object | stooq XAUUSD D-1: `d`, `c` close, `c1`, `c1p`, `rng`, `atr14` (ATR known before `d`), `move_atr` = `c1 / atr14`, `age`. Omitted when stooq is unreachable. |

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

## EA report: `client_payload` of `repository_dispatch` (`event_type: "ea_report"`)

Top level (8 keys, GitHub allows at most 10):

| key | meaning |
|---|---|
| `v`, `kind` | `1`, `"ea_report"` |
| `acct` | 16 hex chars: SHA-256 of `salt:login:server`, computed in the EA. A clear login is never sent (the server would hash and drop it, but it would already be visible in the Actions event log). |
| `rid` | report id `<acct>-<unix>`; all parts of one report share it |
| `part`, `parts` | chunk number and count (trades are split so each request stays under `InpGhMaxPayloadBytes`) |
| `ts` | build time, unix seconds UTC |
| `data` | see below |

`data`: `ea`, `ver`, `reason` (`periodic`, `dayend`, `flag`, `manual`, `deinit`), `phase` (current EA phase),
`eq` {`day_pl_pct`, `eq_day_pct`, `float_pct`, `margin_level`}, `mkt` {`sym`, `bid`, `pc` (prev D1 close),
`d1_open`, `atr` (ATR14 D1), `move`, `move_atr`, `spread`}, `trades[]`, `log[]` (`{t, m}`), `flags[]` (`{key, msg, t}`).

`trades[]` holds one item per **closing deal**:

| key | meaning |
|---|---|
| `deal`, `pos` | deal ticket (upsert key), position id (grouping key) |
| `sym`, `side`, `magic`, `cmt` | symbol, `buy`/`sell` (position side), magic number, entry comment (= method/setup) |
| `vol`, `vol_in` | volume closed by this deal, total entry volume |
| `t_in`, `t_out` | entry and exit time, unix UTC |
| `in`, `out` | entry price, exit price of this deal |
| `sl`, `tp` | SL/TP **at entry** (R uses this SL) |
| `r` | `(out - in) / |in - sl|`, signed by side, for this deal |
| `pl_pct` | net result of this deal (profit + swap + commission + fee) in % of the balance before the deal |
| `risk_pct` | money at risk at entry (entry to SL, full entry volume) in % of balance |
| `phase` | EA phase when the position was opened |
| `reason` | `sl`, `tp`, `so`, `expert`, `client`, `mobile`, `web`, `other` |
| `mae_r`, `mfe_r` | optional: worst / best excursion while open, in R (tracked by `ZGB_OnTick`) |

The server whitelists these keys, drops `balance`, `equity`, `profit`, `pl`, `login`, `account`, `email`, `server`
and similar keys anywhere, masks token-like strings and truncates long strings.

## Stored report files

* `reports/YYYY/MM/DD/<acct>_<YYYYMMDDTHHMMSSZ>[_p<i>of<n>].json`: the sanitised payload, pretty JSON. Re-sending
  an identical payload writes nothing.
* `reports/journal/<acct>/deals.jsonl`: one closing deal per line, upserted by `deal` (idempotent).
* `reports/journal/<acct>/positions.csv`: derived each run from the deals. One row per position: volume-weighted exit
  over partial closes, `r = sign * (exit_vw - entry) / |entry - sl| * vol_out / vol_in`, `pl_pct` summed, `closed` = all
  entry volume closed, `n_exits`, `mae_r`/`mfe_r`.
* `reports/daily/YYYY-MM-DD.md`, `reports/weekly/YYYY-Www.md`, `reports/monthly/YYYY-MM.md`: analytics (see below).
* `docs/dashboard/data.json`: aggregated data for the static dashboard (`docs/dashboard/index.html`).

## Analytics in the summaries

Per account and period (only closed positions, attributed to the UTC date of the last exit):

* trades, wins / losses / break-even (by sign of `pl_pct`), win % (BE excluded), profit factor (gross win % / gross loss %),
  expectancy in R and %, sum R, sum %, average win / loss R, max losing streak, losing streak at period end (and across
  the whole journal);
* drawdown episodes of the additive closed-P/L % curve (depth >= `rules.dd_episode_min_pct`);
* breakdown by method (EA comment), by phase, by magic (when more than one);
* weekday x hour heatmap of entries (weekly / monthly pages);
* rule engine (`config/settings.json` -> `rules`): `no_sl`, `risk_cap` (risk_pct > cap), `news` (entry within
  `news_block_before_min` / `after_min` of NFP/CPI/FOMC), `daily_cap`, `weekly_cap`;
* daily page only: trades table, EA flags, phase/analysis log, last equity and market snapshot.

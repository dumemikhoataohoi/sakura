# Control channel (`control/control.json`)

Claude coordinates the EA through this public file ("auto-apply within bounds"). The EA reads it every
`InpGhControlEveryMin` (60) minutes from raw.githubusercontent, re-validates and clamps it locally, and applies it only
when `InpGhControlEnable=true`.

## Hard limits (enforced inside the EA, never overridable from the repo)

* Risk multipliers are clamped to the EA input **`InpGhMaxRiskMult`** (default **1.0** = the channel can never increase
  risk). The user must raise it locally (max 2.0) to allow boosts.
* The EA's own max risk per trade (EA131 cap), daily/weekly loss stops and crisis layer always apply.
  `ZGB_ApplyRisk(base, max, ...)` returns `min(base x multiplier, max)`.
* The channel never opens or closes trades and never changes SL/TP (the bridge contains no trading calls; a lint test
  checks this).
* Enabling an off-by-default method needs **`InpGhAllowEnable=true`**; lifting the EA's built-in news veto needs
  **`InpGhAllowNewsOverride=true`** (both default false). An extra block always wins over an allow window.
* NEUTRAL (nothing paused, multiplier 1.0, no windows) when the file is missing, invalid, older than 72 h
  (`updated_utc`), or every directive has expired; always NEUTRAL in the Strategy Tester. A network failure keeps the last
  good file (cached in `MQL5\Files\zumi_gh_control.json`), which still expires on its own schedule. A file with a
  lower `seq` than the one applied is ignored (CDN caching can briefly serve an older copy).

## Schema v1

```json
{
  "v": 1, "author": "claude", "seq": 8,
  "updated_utc": "2026-11-25T16:00:00Z", "updated_unix": 1795622400,
  "global": {"risk_mult": 0.5, "pause_all": false, "reason_code": "R05",
             "expires_utc": "2026-11-27T16:00:00Z", "expires_unix": 1795795200},
  "methods": {
    "M001": {"mode": "pause",  "risk_mult": 0.0,  "phases": ["TREND"], "reason_code": "R01", "expires_utc": "...", "expires_unix": 0},
    "M003": {"mode": "boost",  "risk_mult": 1.25, "phases": ["TREND"], "reason_code": "R07", "expires_utc": "...", "expires_unix": 0},
    "M120": {"mode": "on",     "risk_mult": 1.0,  "phases": [],        "reason_code": "R07", "expires_utc": "...", "expires_unix": 0}
  },
  "news_blocks": [
    {"kind": "block", "start_utc": "2026-11-27T13:00:00Z", "end_utc": "2026-11-27T14:00:00Z", "reason_code": "R03", "start_unix": 0, "end_unix": 0},
    {"kind": "allow", "start_utc": "...", "end_utc": "...", "reason_code": "R07", "start_unix": 0, "end_unix": 0}
  ],
  "notes_vi": "Ghi chú công khai ngắn, không có giá."
}
```

| field | rule |
|---|---|
| `seq` | integer, must increase with every change (`control.yml` checks it against the previous commit) |
| `updated_utc` | not in the future; the file is NEUTRAL 72 h after it |
| `*.expires_utc` | default `updated_utc` + 48 h, clamped to + 7 days |
| `global.risk_mult` | [0, 2.0] in the file; EA clamp to `InpGhMaxRiskMult`; a value other than 1.0 needs a reason code |
| `global.pause_all` | pauses every method |
| `global.open_risk_pct` | optional, [1, 20] % of balance: total open-risk cap. The EA applies `min(its local cap input, this)`, so the file can only tighten below, or relax back up to, the user's local cap, never above it. Omitted = the EA's local value. Needs a reason code. Ignored by EAs older than v1.30 |
| `global.side` | optional `both` (default) / `buy` / `sell` / `auto`: the EA opens new entries only on that side (`auto` = the EA's own multi-timeframe Confluence Direction Score, CDS, picks the side: new entries only on the CDS side; when the CDS is FLAT both sides are allowed unless the user set `InpCdsFlatBlocks=true`) (restrict only; never opens, closes or modifies trades). Needs a reason code (R09). Bridge 2.6 / EA161 v1.33: an unknown value makes the whole file invalid (NEUTRAL); expired / stale / tester = `both`. Blocked signals appear in `data.shadow` with reason `SIDE`. Ignored by EAs older than v1.33. Research (rx/ea161v133, nested LOMO): the CDS has no reliable out-of-sample directional edge (AUC ~0.51), so use `buy` / `sell` / `auto` as a risk tool when the multi-TF analysis is clear, not as a profit lever |
| `methods` keys | method **codes** `M###` only (names stay in the user's local map file) |
| `mode` | `pause` (multiplier 0), `reduce` (0 <= x < 1), `on` (1.0; may enable an off-by-default method if the EA allows), `boost` (1 < x <= 2.0, within the EA cap) |
| `phases` | optional list of phase labels where the directive applies (empty = every phase) |
| `news_blocks` | `kind: "block"` (<= 24 h) adds blocking; `kind: "allow"` (<= 4 h, reason R03/R07) can lift the EA's own news veto for one event when `InpGhAllowNewsOverride` is true |
| `reason_code` | one of `control/reason_codes.md` (R00..R08) |
| `notes_vi` | <= 300 chars, public: no prices (decimal numbers or 4+ digit numbers other than years are rejected) |

Effective multiplier for a method in a phase: `global x method` (pause = 0, pause_all = 0), clamped to
`[0, InpGhMaxRiskMult]`.

## Workflow

1. Claude edits `control/control.json` (or runs `python scripts/coordinator_report.py --apply`), bumps `seq`.
2. `python scripts/control.py normalize`: validates, clamps, fills defaults and `*_unix`, checks `seq` against `HEAD`,
   rewrites the file in normalized form and appends one line to `control/CHANGELOG.md`
   (`- <UTC> seq N: M003 pause x0.00 [TREND] until ... (R01); ...`).
3. Commit and push. `control.yml` runs `scripts/control.py check` (valid, normalized, seq increased) and on failure fails
   and comments on the alerts PR. The EA would treat an invalid file as NEUTRAL anyway.
4. `python scripts/control.py effective` shows what the EA applies right now.

## Coordinator (`scripts/coordinator_report.py`)

Writes `reports/coord/YYYY-MM-DD.md` (+ `.json`) with rule hits and a proposed `control.json`, from the newest aggregate
report (rolling-30-day `by_method_phase` stats: count, wins, net R, current losing streak, last-20 trades), the drawdown,
`data/latest.json` flags, `data/quality.json` and the calendar. Pre-registered thresholds (`config/settings.json` >
`coordinator`):

| rule | default | action |
|---|---|---|
| R01 streak | `streak_pause_n` 4 | pause the code in that phase |
| R02 weak record | last-20 win% < `winrate_pause_pct` 35 with >= `winrate_min_trades` 10 | pause in that phase |
| R07 strong record | last-20 n >= 15, win% >= 60, avg R >= 0.3, no current streak | boost x`boost_mult` 1.25 in that phase (effective only if the EA cap allows) |
| R08 recovery | a paused code's last-20 win% >= `recover_winpct` 50, no streak | `on` (lift the pause) |
| R07 enable | codes listed in `enable_codes` (user pre-approved), when the EA's current phase is listed | `on` (needs `InpGhAllowEnable`) |
| R05 drawdown | week or MTD drawdown >= `dd_reduce_pct` 5 % | global x`dd_reduce_mult` 0.5 |
| R04 volatility | VIX high flag and `vix_high_mult` < 1 (off by default) | global x`vix_high_mult` |
| R03 news | NFP/CPI/FOMC within `news_lookahead_h` 72 h | block from `news_before_min` 30 before to `news_after_min` 30 after |

Directives that no longer fire are kept until they expire, then dropped (the method returns to its EA default). Pause wins
over boost for the same code. Nothing is applied without `--apply`; Claude reviews the suggestion and decides.

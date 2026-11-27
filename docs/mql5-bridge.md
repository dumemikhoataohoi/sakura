# MQL5 bridge (`mql5/ZumiGitHubBridge.mqh`)

## Install

1. Copy `mql5/ZumiGitHubBridge.mqh` (and the demo `ZumiGitHubBridge_Demo.mq5`) into `MQL5\Experts\Zumi\` (or `MQL5\Include\`).
2. MT5 > **Tools > Options > Expert Advisors > "Allow WebRequest for listed URL"**, add:
   * `https://raw.githubusercontent.com` (read `latest.json` from a public repo)
   * `https://api.github.com` (send reports; read `latest.json` from a private repo)
   * `https://api.telegram.org` and/or `https://discord.com` only if you enable those channels.
3. In your EA:

```mql5
#include "ZumiGitHubBridge.mqh"

int  OnInit()   { ZGB_Init(); EventSetTimer(10); return(INIT_SUCCEEDED); }
void OnDeinit(const int r) { EventKillTimer(); ZGB_Deinit(); }
void OnTimer()  { ZGB_OnTimer(); ZGB_ReadLatest(g_ctx, false); }   // network only here
void OnTick()   { ZGB_OnTick(); /* strategy; read g_ctx */ }
void OnTradeTransaction(const MqlTradeTransaction &t, const MqlTradeRequest &q, const MqlTradeResult &r)
  { ZGB_OnTradeTransaction(t); }
```

4. Inputs: `InpGhEnable=true`, `InpGhOwner`/`InpGhRepo`, your own private `InpGhAccountSalt`, the token (input
   `InpGhToken` or first line of `MQL5\Files\zumi_gh_token.txt`), and **keep `InpGhReportMode=0`** for this public repo.
   Never put the token in source code or in the repository.
5. Reports count only the host EA's trades when the EA calls `ZGB_SetMagicRange(lo, hi)` before `ZGB_Init()`
   (EA161 uses `InpMagicBase .. InpMagicBase+9999`). Without it (bridge 2.1 behaviour) every trade on the account
   matching `InpGhMagicFilter`/`InpGhSymbolFilter` is counted, including other EAs and manual trades.

## Report modes

| `InpGhReportMode` | what is sent | use |
|---|---|---|
| `0` (default) | **aggregated statistics only** (schema v2 `ea_agg`): for today, the rolling 7 days and month to date: trades, wins, losses, win %, net/avg R, profit factor, max and current losing streak, drawdown % of balance, closed P/L %, rule-break counts, per-method-code and per-phase counts / win % / R, a weekday x 4-hour win % grid (month to date, cells under 3 trades show the count only), optional average MAE/MFE in R; plus equity % (day P/L, equity vs day start, floating) and the gold move in ATR units | public repository |
| `1` | every closing deal with times, prices, SL/TP, tickets, comment, magic, plus phase/analysis log lines (schema v1) | **ONLY for a PRIVATE repository**. The public repo rejects it, and GitHub would still show the payload in the Actions run, so never use it here |

Method names never leave the PC in mode 0: each entry comment is mapped to a code `M001`..`M999` (salted hash of the
name, collisions resolved) and the mapping is appended to `MQL5\Files\zumi_method_codes.csv` (`code,name`). Keep that
file local; it is the only way to read the codes back. An empty comment is `M000`.

**Timing.** In mode 0 a report never leaves when a trade closes. A closing deal marks the current slot
(`InpGhReportEveryMin`, default 60 min, aligned to the clock); the report is built at the start of the next slot and
its `ts` is rounded down to that boundary. EA flags go out on the next 15-minute boundary, and the day-end report at
`InpGhDayEndHour:InpGhDayEndMinute` (rounded to 15 min). So the public timestamps (report time, commit time) only
reveal "something closed in the previous slot", never a trade time.

**Heartbeat (2.4).** At the first timer call of each slot the bridge builds a `heartbeat` aggregate report when nothing
else was built in that slot, no closing deal or flag is pending (those keep their own report at the next boundary, so a
heartbeat never reveals a trade time) and the market context was updated within the last slot + 30 min (no heartbeats
over a closed market). The slot of the last report survives restarts (global variable `ZGB_<acct>_rslot`). A report
over `InpGhMaxPayloadBytes` is rebuilt once without `mkt.ctx`. With an hourly slot this is up to 24 reports a day per
account; the repository stores each one that changed (one commit each) and skips identical heartbeats.

## API

| function | use |
|---|---|
| `ZGB_Init()` | load the token, account hash, cached `latest.json`, method map, remembered events and position extras; disables itself in the Strategy Tester |
| `ZGB_OnTimer()` | scheduler; **at most one WebRequest per call**: chat message > report queue > `latest.json` refresh |
| `ZGB_ReadLatest(ZgbLatest &out, bool allow_network=true)` | fills `out`; returns `false` ("no data": disabled / tester / no data / stale / parse) so the EA can trade normally |
| `ZGB_GetNumber(path, v)`, `ZGB_GetString(path, s)` | any field of the cached JSON, e.g. `"series.T10YIE.c5"`, `"events.next.0.ev"` |
| `ZGB_SetPhase(s)` | current phase (recorded per position at entry, used for per-phase statistics) |
| `ZGB_SetMarketCtx(json)` | 2.4: market-context object sent as `mkt.ctx` in every report (EA161 v1.31 calls it on each new M15 bar). Accepted only as one JSON object <= 1800 bytes of `[A-Za-z0-9_:.,{}[]"-]`; otherwise the last good block is kept. Fields: see `docs/data-dictionary.md` |
| `ZGB_SetShadowStats(json)` | 2.5: blocked-signal shadow statistics sent as `data.shadow` in aggregate reports (EA161 v1.32 calls it on each new M15 bar and at most every 5 min after a change). Same charset rule as the market context, <= 8000 bytes; otherwise the last good block is kept. Fields: see `docs/data-dictionary.md` |
| `ZGB_Log(s)` | phase/analysis log line: sent only in mode 1 (in mode 0 it is discarded at each report) |
| `ZGB_Flag(key, msg)` | EA-raised alert: next report (-> alerts PR comment) and Telegram/Discord. In mode 0 `msg` is published: no prices in it |
| `ZGB_SendReport(reason)` | build a report now (queued; flushed by `ZGB_OnTimer`). In mode 0 prefer the automatic slots |
| `ZGB_OnTick()` | tracks min/max price of open positions for MAE/MFE (no network) |
| `ZGB_OnTradeTransaction(t)` | `DEAL_ADD`: remember the phase at entry; a closing deal marks a report as due |
| `ZGB_Deinit()` | write a pending report to the queue (no network in `OnDeinit`) |

## Control channel (`InpGhControlEnable`, default false)

| function | returns |
|---|---|
| `ZGB_ReadControl(allow_network=true)` | called by `ZGB_OnTimer` (fetch at most every `InpGhControlEveryMin`); true while directives are active |
| `ZGB_RiskMult(methodName, phaseLabel)` | global x method multiplier, clamped to `[0, InpGhMaxRiskMult]`; 1.0 when NEUTRAL |
| `ZGB_ApplyRisk(baseRiskPct, maxRiskPct, methodName, phaseLabel)` | `min(base x multiplier, max)`: the EA's max risk per trade always wins |
| `ZGB_MethodPaused(methodName, phaseLabel)` | true for `pause_all` or a matching `pause` |
| `ZGB_MethodEnabled(methodName, phaseLabel, enabledByDefault)` | false when paused; an off-by-default method runs only with a matching `on` AND `InpGhAllowEnable` |
| `ZGB_ExtraNewsBlock(tUtc)` | inside a `block` window (only adds blocking) |
| `ZGB_NewsAllowed(tUtc)` | inside an `allow` window and `InpGhAllowNewsOverride`; never when a block also covers `tUtc` |

Method names are mapped to codes through the local map file. **Bridge 2.5:** reports (the comment of each position's
entry deal) and control lookups (EA161 v1.32 passes the order comment of the entry it is about to send) both go through
`ZGB_CodeForComment` (cut a `[` broker suffix, keep 31 characters, then `ZGB_MethodCode`: trim, `,` -> space, salted hash),
so a directive written with a report code hits exactly those entries; `phases` are compared with `ZGB_PhaseKey(label)`, the
server's `by_phase` key (`SD|PB|N` -> `SDPBN`, empty -> `none`). Before 2.5 / v1.32 the EA asked about its internal method
name and the raw `D1|H4|vol` label, so pauses from GitHub never matched. Every applied file is logged
(`ZumiGitHubBridge control: M003 pause x0.00 phases[,TREND,] until ... (R01)`), including removed directives and the
switch back to NEUTRAL. Local validation repeats the server rules (version, author, seq, updated time, 72 h staleness,
7-day expiry, modes and bounds, window lengths, reason-code format); any problem means NEUTRAL. The loss stops, the
crisis layer and the max risk per trade stay in the EA's own code: call these functions only to *reduce*, pause, gate
or (within the cap) scale what the EA would do anyway. Inputs: `InpGhControlEnable`, `InpGhControlPath`,
`InpGhControlEveryMin`, `InpGhMaxRiskMult` (1.0), `InpGhAllowEnable` (false), `InpGhAllowNewsOverride` (false).

## Behaviour

* **Statistics source.** Mode 0 recomputes everything from the terminal's deal history at each report (history since
  the earlier of month start and now - 7 days): positions are grouped by position id, only fully closed positions
  count, R uses the SL at entry (the opening order's SL if the deal has none) and is volume-weighted over partial
  closes, % results use the balance just before each exit. Nothing is lost across restarts.
* **Local files** (`MQL5\Files`, or `Common\Files` with `InpGhUseCommonFiles`), none of them belong in the repo:
  `zumi_gh_token.txt` (token), `zumi_method_codes.csv` (method map), `zumi_gh_latest.json` (cache),
  `zumi_gh_events.txt` (NFP/CPI/FOMC times seen in `latest.json`, for the news rule), `zumi_gh_posx.csv` (phase and
  MAE/MFE of closed positions for 45 days, so month-to-date stats stay stable), `zumi_gh_q_*.json` (send queue).
* **Rules counted by the EA:** `no_sl`, `risk_cap` (`OrderCalcProfit` from entry to SL > `InpGhRiskCapPct` % of balance),
  `news` (entry within +-`InpGhNewsBlockMin` minutes of a remembered event; events are known from the time the EA
  first reads `latest.json`), `daily_cap` (server days with closed P/L <= -`InpGhDailyCapPct` %).
* **Timeouts.** `InpGhTimeoutMs` is clamped to 500..5000 ms. WebRequest blocks the EA thread, so the module never runs
  more than one request per `ZGB_OnTimer` call and never touches the network in `OnTick`.
* **Errors.** `-1` from WebRequest: `4014` (URL not allowed) or `4060` (not allowed in this context) switches the network
  off until restart and prints how to fix it; `5200..5203` (invalid URL, connect failed, timeout, request failed) are
  retried with back-off. HTTP `401/403/404` on dispatch: token problem, retry every 30 min; `400/413/422`: payload rejected
  and dropped; `429/5xx`/offline: exponential back-off up to 60 min.
* **Bodies.** Converted with `StringToCharArray(body, data, 0, StringLen(body), CP_UTF8)`, so no trailing `\0` is sent;
  responses are decoded with `CP_UTF8`.
* **Queue.** Each report is a file `zumi_gh_q_<unix>_<part>_<rnd>.json`; one POST every >= 10 s; deleted on HTTP 204;
  capped at 500 files.
* **Payload limits.** `client_payload` may have at most 10 top-level properties (8 are used). GitHub publishes no exact
  byte limit; an aggregate report is a few KB. Detail reports (mode 1) are split into parts under `InpGhMaxPayloadBytes`.
* **Account hash.** `SHA-256(salt + ":" + login + ":" + server)`, first 16 hex chars. With the default salt `change-me` the
  hash of an 8-digit login can be brute-forced: set your own salt and keep it private (it also seeds the method codes).
* **Strategy Tester.** WebRequest does not work there; `ZGB_Init` disables the module and `ZGB_ReadLatest` returns
  `false` ("tester"). For backtests use `data/history/*.csv` and `data/calendar/events_us.csv` offline.

## Not compiled here

The module was reviewed statically (`tests/test_mql5_static.py` lints braces, function definitions, the WebRequest
signature, char-array conversions, `StringFormat` arity, safe defaults, and that the aggregate builder contains no
per-trade fields), but it has **not** been compiled in MetaEditor in this environment. Compile it once and report any
error.

## Open-risk cap (bridge 2.3)

`double ZGB_OpenRiskCap(double local_cap)` returns the EA's total open-risk cap after the control file:
`min(local_cap, global.open_risk_pct)` while the global directive is active, otherwise `local_cap`. It never returns
more than the local cap, so the control channel can only tighten it (or relax it back to the user's own value).
EA161 v1.30 uses it for every new entry.

## Market context and heartbeat (bridge 2.4)

`#define ZGB_HAS_MKTCTX` marks the 2.4 API. EA161 v1.31 builds the context from its analyst panel ([PHASE], [TF],
[CONTEXT], [SCEN], [VOL], [CRISIS]) at each H1 close and refreshes the M15 row on each new M15 bar, then calls
`ZGB_SetMarketCtx`. It is reporting only: no gate, lot size, SL/TP or exit reads it. An EA compiled with a 2.3 bridge
simply sends no context.

## Blocked-signal shadow statistics (bridge 2.5)

`#define ZGB_HAS_SHADOW` marks the 2.5 API. EA161 v1.32 records every signal that reached its order entry and was not
sent (reason codes `CELL CTRL NEWS CRISIS CAP SPREAD BREAKER OTHER`), trades it virtually with the same SL and the TP the
order would have had, resolves it on closed M1 bars only (24 h time-out) and keeps the open and resolved virtual trades in
`MQL5\Files\ZumiMem\<symbol>_<magic base>\shadow132_open.csv` / `shadow132_res.csv` (40 days), so restarts lose nothing.
It hands only counts and R to `ZGB_SetShadowStats`; the bridge adds them as `data.shadow` to aggregate reports (not to
detail reports). A report over `InpGhMaxPayloadBytes` is rebuilt without `mkt.ctx` first, then also without `shadow`.
Reporting only: no gate, lot size, SL/TP or exit reads it. Method codes come from the entry comment through the same
salted-hash map as real trades. An EA compiled with a 2.4 bridge sends no shadow block. v1.32 also removes the v1.31
mode-4 guards `InpClusterMin` / `InpRevVeto`.

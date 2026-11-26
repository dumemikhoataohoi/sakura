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

4. Inputs: `InpGhEnable=true`, `InpGhOwner`/`InpGhRepo`, your own `InpGhAccountSalt`, and the token (input
   `InpGhToken` or first line of `MQL5\Files\zumi_gh_token.txt`). Never put the token in source code or in the repository.

## API

| function | use |
|---|---|
| `ZGB_Init()` | load token, compute the account hash, load the cached `latest.json`; disables itself in the Strategy Tester |
| `ZGB_OnTimer()` | scheduler; **at most one WebRequest per call**: chat message > report queue > `latest.json` refresh |
| `ZGB_ReadLatest(ZgbLatest &out, bool allow_network=true)` | fills `out`; returns `false` ("no data": disabled / tester / no data / stale / parse) so the EA can trade normally |
| `ZGB_GetNumber(path, v)`, `ZGB_GetString(path, s)` | any field of the cached JSON, e.g. `"series.T10YIE.c5"`, `"events.next.0.ev"` |
| `ZGB_SetPhase(s)`, `ZGB_Log(s)` | current phase (stored per position at entry) and phase/analysis log lines (sent with the next report) |
| `ZGB_Flag(key, msg)` | EA-raised alert: goes into the next report (-> alerts PR comment, within ~5 min) and to Telegram/Discord |
| `ZGB_SendReport(reason)` | build a report now (it is queued; the queue is flushed by `ZGB_OnTimer`) |
| `ZGB_OnTick()` | tracks min/max price of open positions for MAE/MFE (no network) |
| `ZGB_OnTradeTransaction(t)` | `DEAL_ADD`: remember the phase at entry, mark a report due on exits |
| `ZGB_Deinit()` | write pending trades/log lines to the queue (no network in `OnDeinit`) |

## Behaviour

* **Timeouts.** `InpGhTimeoutMs` is clamped to 500..5000 ms. WebRequest blocks the EA thread, so the module never
  runs more than one request per `ZGB_OnTimer` call and never touches the network in `OnTick`.
* **Errors.** `-1` from WebRequest: `4014` (URL not allowed) or `4060` (not allowed in this context) switches the network
  off until restart and prints how to fix it; `5200..5203` (invalid URL, connect failed, timeout, request failed) are
  retried with back-off. HTTP `401/403/404` on dispatch: token problem, retry every 30 min; `400/413/422`: payload rejected
  and dropped; `429/5xx`/offline: exponential back-off up to 60 min.
* **Bodies.** Converted with `StringToCharArray(body, data, 0, StringLen(body), CP_UTF8)`, so no trailing `\0` is sent
  (GitHub rejects it as invalid JSON); responses are decoded with `CP_UTF8`.
* **Reports.** Triggered by closing deals (`OnTradeTransaction`), batched and rate-limited: a periodic report at most every
  `InpGhReportEveryMin` (30) minutes when something closed, an EA flag report at most every 5 minutes, and one day-end
  report at `InpGhDayEndHour:InpGhDayEndMinute` server time (always, with the equity snapshot). Deals are found from the
  history (ticket > last reported ticket, kept in terminal global variables `ZGB_<hash>_ld_k/_ld_t`), so nothing is lost
  across restarts.
* **Queue.** Each report part is a file `MQL5\Files\zumi_gh_q_<unix>_<part>_<rnd>.json`; one POST every >= 10 s; deleted
  on HTTP 204. Capped at 500 files.
* **Chunking.** `client_payload` may have at most 10 top-level properties (we use 8). GitHub publishes no exact byte
  limit for it, and large requests are rejected, so each request is kept under `InpGhMaxPayloadBytes` (default 30000):
  trades are split greedily across parts sharing one `rid`; part 1 also carries the log and flags (log trimmed to fit,
  newest lines kept).
* **Values.** `pl_pct` is the deal's net result over the balance just before it; `risk_pct` uses `OrderCalcProfit`
  from entry to the entry SL; R uses the entry SL (the opening order's SL if the deal has none). No balance, equity or
  money amount is sent.
* **Account hash.** `SHA-256(salt + ":" + login + ":" + server)`, first 16 hex chars. With the default salt
  `change-me` the hash of an 8-digit login can be brute-forced: set your own salt and keep it private.
* **Strategy Tester.** WebRequest does not work there; `ZGB_Init` disables the module and `ZGB_ReadLatest` returns
  `false` ("tester"). For backtests use `data/history/*.csv` and `data/calendar/events_us.csv` offline.

## Not compiled here

The module was reviewed statically (and `tests/test_mql5_static.py` lints braces, function definitions, the WebRequest
signature, char-array conversions and `StringFormat` arity), but it has **not** been compiled in MetaEditor in this
environment. Compile it once and report any error.

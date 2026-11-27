//+------------------------------------------------------------------+
//|                                             ZumiGitHubBridge.mqh |
//|  Zumi XAU EA <-> GitHub repo "sakura" data bot                   |
//|                                                                  |
//|  READ : data/latest.json (macro / calendar context) -> ZGB_ReadLatest()
//|  WRITE: reports -> repository_dispatch "ea_report"              |
//|         (ZGB_SendReport / automatic from ZGB_OnTimer)            |
//|   InpGhReportMode = 0 (default, PUBLIC repo): AGGREGATED stats   |
//|     only (schema v2 "ea_agg"): per period day / rolling week /   |
//|     month to date; no trades, times, prices, SL/TP or tickets;   |
//|     methods as codes M001.. (map kept ONLY in MQL5\Files).      |
//|   InpGhReportMode = 1: full per-trade detail (schema v1) - ONLY  |
//|     for a PRIVATE repo (a public repo rejects it).               |
//|  OPTIONAL instant channels: Telegram bot, Discord webhook (off)  |
//|  CONTROL (off by default, InpGhControlEnable): reads             |
//|  control/control.json hourly -> ZGB_RiskMult / ZGB_MethodPaused /|
//|  ZGB_MethodEnabled / ZGB_ExtraNewsBlock / ZGB_NewsAllowed.       |
//|  2.3: ZGB_OpenRiskCap(local) = min(local, global.open_risk_pct)  |
//|  while the global block is active (only tightens, never > local)|
//|  2.4: ZGB_SetMarketCtx(json) -> mkt.ctx in every report (EA     |
//|  market context: bucketed / relative values only, no prices),   |
//|  plus a "heartbeat" report once per InpGhReportEveryMin slot    |
//|  while the context is fresh and nothing else was sent (reporting|
//|  only: the bridge still never trades).                          |
//|  2.5: ZGB_SetShadowStats(json) -> data.shadow in aggregate      |
//|  reports: counts / R of the EA's blocked signals traded         |
//|  virtually (by reason, method code x reason, phase x reason; no |
//|  prices, times or method names). Reporting only.                |
//|  2.6: global.side = both / buy / sell / auto (R09): direction    |
//|  bias for NEW entries only. ZGB_SideAllowed(side) = true when the |
//|  control is NEUTRAL (inactive / stale / expired / tester), when   |
//|  side is both or auto (auto = the EA's own confluence score      |
//|  decides: ZGB_SideMode() == ZGB_SIDE_AUTO), or when it matches.   |
//|  Never opens, closes or modifies a position.                     |
//|  2.7: aggregate blocks gain by_side ("buy" / "sell": coordinator |
//|  rule R09) and by_session (ASIA / LON / NY / OFF by the UTC hour |
//|  of the entry, bands ZGB_SESS_*_H). ZGB_SessionOf(t_utc) labels  |
//|  any time the same way (EA shadow cells "SESS:REASON"). Extra    |
//|  dimension only: ZGB_PhaseKey and the control codes are         |
//|  unchanged. Reporting only, no new inputs.                       |
//|  2.5 FIX: ZGB_CodeForComment(cmt) is the one comment -> M### path|
//|  for reports (entry deal comment) AND control (the EA passes the |
//|  order comment); control phases are keyed like the server       |
//|  (ZGB_PhaseKey: "SD|PB|N" -> "SDPBN"). Before 2.5 a pause from   |
//|  GitHub never matched the report codes.                         |
//|  HARD LIMITS stay in the EA: multipliers are clamped to          |
//|  InpGhMaxRiskMult (default 1.0 = never increase); the EA's max   |
//|  risk per trade, daily/weekly loss stops and crisis layer are    |
//|  never overridable; no remote open/close; no SL/TP changes;      |
//|  missing/invalid/expired/stale (>72 h) control = NEUTRAL;        |
//|  Strategy Tester = NEUTRAL.                                      |
//|                                                                  |
//|  SETUP (MT5 > Tools > Options > Expert Advisors >                |
//|         "Allow WebRequest for listed URL"), add:                 |
//|     https://raw.githubusercontent.com                            |
//|     https://api.github.com                                       |
//|     https://api.telegram.org      (only if Telegram is enabled)  |
//|     https://discord.com           (only if Discord is enabled)   |
//|                                                                  |
//|  RULES THIS MODULE FOLLOWS                                       |
//|  * WebRequest is synchronous: every call uses a short timeout    |
//|    (InpGhTimeoutMs, clamped to 500..5000 ms) and ZGB_OnTimer()   |
//|    makes at most ONE request per call. Call it from OnTimer,     |
//|    never from OnTick.                                            |
//|  * Fail safe: on any error or stale data ZGB_ReadLatest()        |
//|    returns false ("no data") and the EA trades normally.         |
//|  * Strategy Tester: WebRequest is not available there, so the    |
//|    module switches itself off (ReadLatest -> false, no sends).   |
//|  * Privacy: the account is sent only as a salted SHA-256 hash;   |
//|    money only as % of balance; the token is never printed. In    |
//|    aggregate mode reports leave only at slot boundaries          |
//|    (InpGhReportEveryMin) with the timestamp rounded to the slot, |
//|    so report times do not reveal trade times.                    |
//|  * repository_dispatch limits: client_payload may have at most   |
//|    10 top-level keys (we use 8). GitHub does not publish an exact |
//|    byte limit, large bodies are rejected, so each request is kept |
//|    <= InpGhMaxPayloadBytes (default 30000) and trades are split  |
//|    into parts (part/parts/rid) that the server re-joins.         |
//|  * Offline: reports are queued as files in MQL5\Files            |
//|    (zumi_gh_q_*.json) and flushed later with back-off.           |
//|                                                                  |
//|  The fine-grained PAT needs: only repository <owner>/<repo>,     |
//|  permission "Contents: Read and write" (dispatches require it).  |
//|  Put it in InpGhToken or in the file MQL5\Files\zumi_gh_token.txt |
//|  (first line). Never commit it anywhere.                         |
//+------------------------------------------------------------------+
#ifndef ZUMI_GITHUB_BRIDGE_MQH
#define ZUMI_GITHUB_BRIDGE_MQH

#define ZGB_VERSION "2.7"
#define ZGB_HAS_MKTCTX                 // 2.4: ZGB_SetMarketCtx available (EA161 v1.31 tests this)
#define ZGB_CTX_MAX 1800               // 2.4: max bytes of the mkt.ctx object
#define ZGB_HAS_SHADOW                 // 2.5: ZGB_SetShadowStats available (EA161 v1.32 tests this)
#define ZGB_SHADOW_MAX 8000            // 2.5: max bytes of the data.shadow object
#define ZGB_HAS_SIDE                   // 2.6: global.side direction bias (ZGB_SideMode / ZGB_SideAllowed; EA161 v1.33 tests this)
#define ZGB_SIDE_BOTH  0               // 2.6: global.side values (= sakura scripts/control.py SIDES)
#define ZGB_SIDE_BUY   1
#define ZGB_SIDE_SELL  (-1)
#define ZGB_SIDE_AUTO  2
#define ZGB_HAS_BYSIDE                 // 2.7: aggregate blocks carry by_side {"buy","sell"} (sakura agg.AGG_SIDES, coordinator R09)
#define ZGB_HAS_SESSION                // 2.7: by_session aggregate + ZGB_SessionOf(t_utc) (sakura agg.SESSION_BANDS_UTC)
#define ZGB_SESS_ASIA_H 0              // 2.7: session = band of the UTC hour: ASIA [0,8), LON [8,12), NY [12,20), OFF [20,24);
#define ZGB_SESS_LON_H  8              //      every band starts on a 4-hour grid bucket, so by_session never reveals more than the
#define ZGB_SESS_NY_H   12             //      month-to-date grid already does (no single-hour intersection)
#define ZGB_SESS_OFF_H  20
#define ZGB_CTL_CACHE   "zumi_gh_control.json"
#define ZGB_CTL_STALE_S (72 * 3600)
#define ZGB_CTL_MAX_TTL (7 * 86400)
#define ZGB_CTL_MAXMULT 2.0
#define ZGB_ORP_MIN 1.0                // 2.3: global.open_risk_pct valid range (% of balance), = sakura scripts/control.py
#define ZGB_ORP_MAX 20.0

input group "=== Zumi GitHub bridge ==="
input bool   InpGhEnable          = false;               // Enable the GitHub bridge
input string InpGhOwner           = "dumemikhoataohoi";  // GitHub owner
input string InpGhRepo            = "sakura";            // GitHub repository
input string InpGhBranch          = "main";              // Branch holding data/latest.json
input string InpGhToken           = "";                  // Fine-grained PAT (empty = read token file)
input string InpGhTokenFile       = "zumi_gh_token.txt"; // Token file in MQL5\Files (first line)
input bool   InpGhPrivateRepo     = false;               // Private repo: read via API contents + token
input string InpGhLatestPath      = "data/latest.json";  // Path of latest.json in the repo
input string InpGhRawBase         = "https://raw.githubusercontent.com"; // Raw content base URL
input string InpGhApiBase         = "https://api.github.com";            // GitHub API base URL
input int    InpGhTimeoutMs       = 3000;                // WebRequest timeout ms (500..5000)
input int    InpGhReadEveryMin    = 30;                  // Re-download latest.json every N minutes
input int    InpGhMaxAgeHours     = 36;                  // latest.json older than this = no data
input int    InpGhReportMode      = 0;                   // 0 = aggregate stats (public repo), 1 = full detail (ONLY for a PRIVATE repo)
input int    InpGhReportEveryMin  = 60;                  // Report slot in minutes (sent at slot boundaries)
input int    InpGhDayEndHour      = 23;                  // Day-end report, server hour
input int    InpGhDayEndMinute    = 45;                  // Day-end report, server minute
input string InpGhAccountSalt     = "change-me";         // Salt for account hash + method codes (keep private)
input string InpGhMethodMapFile   = "zumi_method_codes.csv"; // Local method->code map in MQL5\Files (never commit)
input double InpGhRiskCapPct      = 1.0;                 // Rule: risk per trade above this % counts as a break
input double InpGhDailyCapPct     = 3.0;                 // Rule: a day with closed P/L <= -this % counts as a break
input int    InpGhNewsBlockMin    = 15;                  // Rule: entry within +-N min of NFP/CPI/FOMC is a break
input long   InpGhMagicFilter     = -1;                  // Report only this magic (-1 = all)
input string InpGhSymbolFilter    = "";                  // Report only this symbol ("" = all)
input string InpGhMarketSymbol    = "";                  // Symbol for market context ("" = chart)
input int    InpGhMaxPayloadBytes = 30000;               // Max bytes per dispatch request
input bool   InpGhUseCommonFiles  = false;               // Cache/queue in Common\Files
input group "=== Control channel (Claude coordination, default off) ==="
input bool   InpGhControlEnable     = false;             // Apply directives from control/control.json
input string InpGhControlPath       = "control/control.json"; // Path of the control file in the repo
input int    InpGhControlEveryMin   = 60;                // Re-download the control file every N minutes
input double InpGhMaxRiskMult       = 1.0;               // HARD cap of remote risk multipliers (1.0 = never increase, max 2.0)
input bool   InpGhAllowEnable       = false;             // Allow mode "on" to enable methods that are off by default
input bool   InpGhAllowNewsOverride = false;             // Allow "allow" windows to lift the EA's own news veto
input group "=== Optional instant channels (default off) ==="
input bool   InpTgEnable          = false;               // Telegram: send EA flags / alerts
input string InpTgBotToken        = "";                  // Telegram bot token
input string InpTgChatId          = "";                  // Telegram chat id
input bool   InpDcEnable          = false;               // Discord: send EA flags / alerts
input string InpDcWebhookUrl      = "";                  // Discord webhook URL

//--- latest.json fields the EA usually needs (EMPTY_VALUE = missing)
struct ZgbLatest
  {
   bool              ok;            // true = fresh data
   string            reason;        // why not ok: disabled / tester / no data / stale / parse
   datetime          generated;     // generation time (UTC)
   double            age_h;         // hours since generation
   double            dgs10, dgs2, dfii10, t10yie, usd, vix, oil, spx;
   double            vix_chg_pct;   // last daily VIX change in %
   double            dfii10_chg;    // last daily real-yield change (percentage points)
   string            vix_date;      // observation date of the VIX value (D-1 or older)
   int               vix_high, vix_jump;
   string            next_event;    // NFP / CPI / FOMC
   datetime          next_event_utc;
   int               next_event_unc;// 1 = date not confirmed
   double            gold_move_atr; // optional stooq D-1 gold move / ATR14
  };

//--- internal state
long     g_zgbMagicLo    = -1;      // 2.2: report only magics in [lo, hi] (set by the EA, ZGB_SetMagicRange); -1 = no range
long     g_zgbMagicHi    = -1;
string   g_zgbToken      = "";
string   g_zgbAcct       = "";
bool     g_zgbNetOk      = false;   // false in tester / after a 4014/4060 error
bool     g_zgbInTimer    = false;
int      g_zgbBudget     = 0;       // requests left in this ZGB_OnTimer call
string   g_zgbLatest     = "";      // cached latest.json text
datetime g_zgbNextRead   = 0;       // GMT
int      g_zgbReadFails  = 0;
datetime g_zgbLastReport = 0;       // GMT of the last periodic report build
datetime g_zgbLastFlagReport = 0;
int      g_zgbDayEndKey  = 0;       // yyyymmdd (server) of the last day-end report
datetime g_zgbPostAfter  = 0;       // GMT: back-off for dispatch POSTs
int      g_zgbPostFails  = 0;
datetime g_zgbLastPost   = 0;
bool     g_zgbDirty      = false;   // a closing deal happened since the last report
bool     g_zgbWarned401  = false;
string   g_zgbPhase      = "";
string   g_zgbLogMsg[];
datetime g_zgbLogT[];
string   g_zgbFlagJson[];
string   g_zgbTgQ[];                // Telegram queue
string   g_zgbDcQ[];                // Discord queue
long     g_zgbTrkPos[];             // MAE/MFE tracker, keyed by position identifier
double   g_zgbTrkMin[];
double   g_zgbTrkMax[];
string   g_zgbTrkPhase[];
datetime g_zgbLastTrack  = 0;
long     g_zgbDirtySlot  = -1;      // report slot in which the last closing deal was seen
long     g_zgbFlagSlot   = -1;      // 15-minute slot in which the first pending flag was raised
string   g_zgbCtx        = "";      // 2.4: last valid market-context object (mkt.ctx)
datetime g_zgbCtxT       = 0;       // 2.4: GMT when it was set
bool     g_zgbCtxWarned  = false;
string   g_zgbShadow     = "";      // 2.5: last valid blocked-signal shadow statistics object (data.shadow)
bool     g_zgbShadowWarned = false;
long     g_zgbRptSlot    = -1;      // 2.4: report slot (InpGhReportEveryMin) of the last report built (heartbeat: at most one report per slot)
string   g_zgbMapName[];            // method name -> code (local file only)
string   g_zgbMapCode[];
long     g_zgbEvT[];                // NFP/CPI/FOMC times (UTC) seen in latest.json, for the news rule

struct ZgbPos                       // one CLOSED position, used only inside the EA for aggregation
  {
   long              id;
   datetime          t_in;          // server time of the first entry
   datetime          t_out;         // server time of the last exit
   double            r;             // volume-weighted R (entry SL)
   bool              has_r;
   double            pl;            // net result, % of balance
   string            mcode;         // method code M000..M999
   string            phase;
   int               side;          // 2.7: 1 = buy, -1 = sell (direction of the entry deal)
   string            sess;          // 2.7: ASIA / LON / NY / OFF of the entry time (UTC)
   bool              no_sl, risk_over, news;
   double            mae, mfe;
   bool              has_ex;
  };

#define ZGB_MAX_LOG    120
#define ZGB_MAX_FLAGS  20
#define ZGB_MAX_QUEUE  500
#define ZGB_MAX_TRACK  200
#define ZGB_CACHE_FILE "zumi_gh_latest.json"
#define ZGB_QUEUE_GLOB "zumi_gh_q_*.json"
#define ZGB_EVENTS_FILE "zumi_gh_events.txt"
#define ZGB_MIN_CELL_N 3

//+------------------------------------------------------------------+
//| small helpers                                                    |
//+------------------------------------------------------------------+
int ZGB_FileFlags(void) { return(InpGhUseCommonFiles ? FILE_COMMON : 0); }

int ZGB_Timeout(void)
  {
   int t = InpGhTimeoutMs;
   if(t < 500)  t = 500;
   if(t > 5000) t = 5000;
   return(t);
  }

string ZGB_Esc(const string s)
  {
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(s, i);
      if(c == '"')       out += "\\\"";
      else if(c == '\\') out += "\\\\";
      else if(c == '\n') out += "\\n";
      else if(c == '\r') out += "\\r";
      else if(c == '\t') out += "\\t";
      else if(c < 0x20)  out += StringFormat("\\u%04x", (int)c);
      else               out += ShortToString(c);
     }
   return(out);
  }

string ZGB_Num(const double v, const int digits)
  {
   if(v == EMPTY_VALUE || !MathIsValidNumber(v)) return("null");
   return(DoubleToString(v, digits));
  }

int ZGB_Utf8Len(const string s)
  {
   uchar tmp[];
   return(StringToCharArray(s, tmp, 0, StringLen(s), CP_UTF8));
  }

string ZGB_Trim(string s)
  {
   StringTrimLeft(s);
   StringTrimRight(s);
   return(s);
  }

// Hide anything secret before printing a URL (Telegram URLs embed the bot token).
string ZGB_MaskUrl(const string url)
  {
   int p = StringFind(url, "/bot");
   if(p > 0) return(StringSubstr(url, 0, p) + "/bot***");
   p = StringFind(url, "/webhooks/");
   if(p > 0) return(StringSubstr(url, 0, p) + "/webhooks/***");
   return(url);
  }

long ZGB_ServerOffset(void)
  {
   // server time - UTC, rounded to 15 minutes (Exness MT5 servers run on UTC, offset 0)
   long off = (long)(TimeTradeServer() - TimeGMT());
   return((long)MathRound(off / 900.0) * 900);
  }

long ZGB_ToUtc(const datetime server_time) { return((long)server_time - ZGB_ServerOffset()); }

//+------------------------------------------------------------------+
//| HTTP                                                             |
//+------------------------------------------------------------------+
void ZGB_WebError(const int err, const string url)
  {
   string u = ZGB_MaskUrl(url);
   if(err == 4014 || err == 4060)
     {
      // 4014: URL not in the allowed list / function not allowed; 4060: not allowed in this context
      g_zgbNetOk = false;
      string msg = StringFormat("ZumiGitHubBridge: WebRequest blocked (error %d) for %s. Add https://raw.githubusercontent.com "
                                "and https://api.github.com to Tools > Options > Expert Advisors > 'Allow WebRequest for listed URL'. "
                                "Network disabled until the EA is restarted.", err, u);
      Print(msg);
      Alert(msg);
      return;
     }
   string what = "unknown";
   if(err == 5200) what = "invalid URL";
   else if(err == 5201) what = "connection failed";
   else if(err == 5202) what = "timeout";
   else if(err == 5203) what = "request failed";
   PrintFormat("ZumiGitHubBridge: WebRequest error %d (%s) for %s", err, what, u);
  }

// Returns the HTTP status, -1 on transport error, -2 when the network is not allowed.
int ZGB_Http(const string method, const string url, const string headers, const string body, string &resp)
  {
   resp = "";
   if(!g_zgbNetOk) return(-2);
   if(g_zgbInTimer)
     {
      if(g_zgbBudget <= 0) return(-2);
      g_zgbBudget--;
     }
   char data[];
   char result[];
   string resp_headers = "";
   ArrayResize(data, 0);
   int len = StringLen(body);
   if(len > 0)
      StringToCharArray(body, data, 0, len, CP_UTF8);   // count = StringLen: no trailing '\0' is sent
   ResetLastError();
   int code = WebRequest(method, url, headers, ZGB_Timeout(), data, result, resp_headers);
   if(code == -1)
     {
      ZGB_WebError(GetLastError(), url);
      return(-1);
     }
   if(ArraySize(result) > 0)
      resp = CharArrayToString(result, 0, ArraySize(result), CP_UTF8);
   return(code);
  }

string ZGB_GhHeaders(const bool with_token, const string accept)
  {
   string h = "Accept: " + accept + "\r\n" +
              "X-GitHub-Api-Version: 2022-11-28\r\n" +
              "User-Agent: zumi-ea-bridge\r\n";
   if(with_token && StringLen(g_zgbToken) > 0)
      h += "Authorization: Bearer " + g_zgbToken + "\r\n";
   return(h);
  }

//+------------------------------------------------------------------+
//| minimal JSON path extractor ("series.VIXCLS.v", "events.next.0.t")|
//+------------------------------------------------------------------+
int ZGB_JWs(const string &s, int i)
  {
   int n = StringLen(s);
   while(i < n)
     {
      ushort c = StringGetCharacter(s, i);
      if(c != ' ' && c != '\t' && c != '\n' && c != '\r') break;
      i++;
     }
   return(i);
  }

int ZGB_JSkipStr(const string &s, int i)   // s[i] == '"'; returns index after the closing quote
  {
   int n = StringLen(s);
   i++;
   while(i < n)
     {
      ushort c = StringGetCharacter(s, i);
      if(c == '\\') { i += 2; continue; }
      if(c == '"') return(i + 1);
      i++;
     }
   return(-1);
  }

int ZGB_JSkipVal(const string &s, int i)
  {
   int n = StringLen(s);
   i = ZGB_JWs(s, i);
   if(i >= n) return(-1);
   ushort c = StringGetCharacter(s, i);
   if(c == '"') return(ZGB_JSkipStr(s, i));
   if(c == '{' || c == '[')
     {
      int depth = 0;
      while(i < n)
        {
         c = StringGetCharacter(s, i);
         if(c == '"') { i = ZGB_JSkipStr(s, i); if(i < 0) return(-1); continue; }
         if(c == '{' || c == '[') depth++;
         else if(c == '}' || c == ']') { depth--; if(depth == 0) return(i + 1); }
         i++;
        }
      return(-1);
     }
   while(i < n)
     {
      c = StringGetCharacter(s, i);
      if(c == ',' || c == '}' || c == ']' || c == ' ' || c == '\t' || c == '\r' || c == '\n') break;
      i++;
     }
   return(i);
  }

int ZGB_JKey(const string &s, int obj, const string key)   // s[obj] == '{'; returns value start or -1
  {
   int n = StringLen(s);
   int i = obj + 1;
   while(i < n)
     {
      i = ZGB_JWs(s, i);
      if(i >= n || StringGetCharacter(s, i) != '"') return(-1);
      int ke = ZGB_JSkipStr(s, i);
      if(ke < 0) return(-1);
      string k = StringSubstr(s, i + 1, ke - i - 2);
      i = ZGB_JWs(s, ke);
      if(i >= n || StringGetCharacter(s, i) != ':') return(-1);
      i = ZGB_JWs(s, i + 1);
      if(k == key) return(i);
      i = ZGB_JSkipVal(s, i);
      if(i < 0) return(-1);
      i = ZGB_JWs(s, i);
      if(i >= n) return(-1);
      ushort c = StringGetCharacter(s, i);
      if(c == ',') { i++; continue; }
      return(-1);   // '}' -> key not found
     }
   return(-1);
  }

int ZGB_JIdx(const string &s, int arr, const int idx)   // s[arr] == '['
  {
   int n = StringLen(s);
   int i = arr + 1;
   int k = 0;
   while(i < n)
     {
      i = ZGB_JWs(s, i);
      if(i >= n || StringGetCharacter(s, i) == ']') return(-1);
      if(k == idx) return(i);
      i = ZGB_JSkipVal(s, i);
      if(i < 0) return(-1);
      i = ZGB_JWs(s, i);
      if(i >= n || StringGetCharacter(s, i) != ',') return(-1);
      i++;
      k++;
     }
   return(-1);
  }

int ZGB_JPath(const string &s, const string path)
  {
   string parts[];
   int np = StringSplit(path, '.', parts);
   int i = ZGB_JWs(s, 0);
   for(int p = 0; p < np && i >= 0; p++)
     {
      if(i >= StringLen(s)) return(-1);
      ushort c = StringGetCharacter(s, i);
      if(c == '{')      i = ZGB_JKey(s, i, parts[p]);
      else if(c == '[') i = ZGB_JIdx(s, i, (int)StringToInteger(parts[p]));
      else              return(-1);
     }
   return(i);
  }

int ZGB_Hex4(const string h)
  {
   int v = 0;
   for(int i = 0; i < 4 && i < StringLen(h); i++)
     {
      ushort c = StringGetCharacter(h, i);
      int d = -1;
      if(c >= '0' && c <= '9') d = c - '0';
      else if(c >= 'a' && c <= 'f') d = c - 'a' + 10;
      else if(c >= 'A' && c <= 'F') d = c - 'A' + 10;
      if(d < 0) return('?');
      v = v * 16 + d;
     }
   return(v);
  }

string ZGB_JUnescape(const string raw)
  {
   string out = "";
   int n = StringLen(raw);
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(raw, i);
      if(c != '\\' || i + 1 >= n) { out += ShortToString(c); continue; }
      ushort e = StringGetCharacter(raw, ++i);
      if(e == 'n') out += "\n";
      else if(e == 't') out += "\t";
      else if(e == 'r') out += "\r";
      else if(e == 'b' || e == 'f') out += " ";
      else if(e == 'u' && i + 4 < n)
        {
         out += ShortToString((ushort)ZGB_Hex4(StringSubstr(raw, i + 1, 4)));
         i += 4;
        }
      else out += ShortToString(e);   // \" \\ \/
     }
   return(out);
  }

bool ZGB_JNumber(const string &s, const string path, double &v)
  {
   int i = ZGB_JPath(s, path);
   if(i < 0) return(false);
   ushort c = StringGetCharacter(s, i);
   if(c == '"' || c == '{' || c == '[' || c == 'n') return(false);   // string / object / null
   int e = ZGB_JSkipVal(s, i);
   if(e <= i) return(false);
   string tok = StringSubstr(s, i, e - i);
   if(tok == "true")  { v = 1; return(true); }
   if(tok == "false") { v = 0; return(true); }
   v = StringToDouble(tok);
   return(MathIsValidNumber(v));
  }

bool ZGB_JString(const string &s, const string path, string &out)
  {
   int i = ZGB_JPath(s, path);
   if(i < 0) return(false);
   int e = ZGB_JSkipVal(s, i);
   if(e <= i) return(false);
   if(StringGetCharacter(s, i) == '"')
      out = ZGB_JUnescape(StringSubstr(s, i + 1, e - i - 2));
   else
      out = StringSubstr(s, i, e - i);
   return(true);
  }

double ZGB_JNumOr(const string &s, const string path, const double dflt)
  {
   double v;
   return(ZGB_JNumber(s, path, v) ? v : dflt);
  }

//+------------------------------------------------------------------+
//| small file helpers                                               |
//+------------------------------------------------------------------+
bool ZGB_WriteFile(const string name, const string text)
  {
   int h = FileOpen(name, FILE_WRITE | FILE_BIN | ZGB_FileFlags());
   if(h == INVALID_HANDLE) return(false);
   uchar arr[];
   int n = StringToCharArray(text, arr, 0, StringLen(text), CP_UTF8);
   if(n > 0) FileWriteArray(h, arr, 0, n);
   FileClose(h);
   return(true);
  }

bool ZGB_ReadFile(const string name, string &text)
  {
   text = "";
   if(!FileIsExist(name, ZGB_FileFlags())) return(false);
   int h = FileOpen(name, FILE_READ | FILE_BIN | FILE_SHARE_READ | ZGB_FileFlags());
   if(h == INVALID_HANDLE) return(false);
   int size = (int)FileSize(h);
   uchar arr[];
   int n = (size > 0) ? (int)FileReadArray(h, arr, 0, size) : 0;
   FileClose(h);
   if(n > 0) text = CharArrayToString(arr, 0, n, CP_UTF8);
   return(n > 0);
  }

//+------------------------------------------------------------------+
//| init / deinit                                                    |
//+------------------------------------------------------------------+
string ZGB_AccountHash(void)
  {
   string src = InpGhAccountSalt + ":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ":" +
                AccountInfoString(ACCOUNT_SERVER);
   uchar data[], key[], res[];
   StringToCharArray(src, data, 0, StringLen(src), CP_UTF8);
   ArrayResize(key, 0);
   if(CryptEncode(CRYPT_HASH_SHA256, data, key, res) < 8) return("unknown");
   string hex = "";
   for(int i = 0; i < 8; i++) hex += StringFormat("%02x", (int)res[i]);
   return(hex);   // 16 hex chars, never the login itself
  }

void ZGB_LoadToken(void)
  {
   g_zgbToken = ZGB_Trim(InpGhToken);
   if(StringLen(g_zgbToken) > 0 || StringLen(InpGhTokenFile) == 0) return;
   if(!FileIsExist(InpGhTokenFile, ZGB_FileFlags())) return;
   int h = FileOpen(InpGhTokenFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | ZGB_FileFlags());
   if(h == INVALID_HANDLE) return;
   if(!FileIsEnding(h)) g_zgbToken = ZGB_Trim(FileReadString(h));
   FileClose(h);
  }

string ZGB_GvName(const string what) { return("ZGB_" + g_zgbAcct + "_" + what); }

//+------------------------------------------------------------------+
//| method name -> code (M001..M999). The mapping lives ONLY in the  |
//| local file InpGhMethodMapFile ("code,name" per line); codes are  |
//| derived from a salted hash, so they are stable even if the file  |
//| is lost (collisions are resolved by the file).                   |
//+------------------------------------------------------------------+
void ZGB_MapLoad(void)
  {
   ArrayResize(g_zgbMapName, 0);
   ArrayResize(g_zgbMapCode, 0);
   if(StringLen(InpGhMethodMapFile) == 0 || !FileIsExist(InpGhMethodMapFile, ZGB_FileFlags())) return;
   int h = FileOpen(InpGhMethodMapFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | ZGB_FileFlags());
   if(h == INVALID_HANDLE) return;
   while(!FileIsEnding(h))
     {
      string line = ZGB_Trim(FileReadString(h));
      int c = StringFind(line, ",");
      if(c != 4 || StringSubstr(line, 0, 1) != "M") continue;
      int n = ArraySize(g_zgbMapName);
      ArrayResize(g_zgbMapName, n + 1);
      ArrayResize(g_zgbMapCode, n + 1);
      g_zgbMapCode[n] = StringSubstr(line, 0, 4);
      g_zgbMapName[n] = StringSubstr(line, 5);
     }
   FileClose(h);
  }

bool ZGB_CodeUsed(const string code)
  {
   for(int i = 0; i < ArraySize(g_zgbMapCode); i++) if(g_zgbMapCode[i] == code) return(true);
   return(false);
  }

string ZGB_MethodCode(const string method)
  {
   string name = ZGB_Trim(method);
   StringReplace(name, ",", " ");
   if(StringLen(name) == 0) return("M000");
   for(int i = 0; i < ArraySize(g_zgbMapName); i++) if(g_zgbMapName[i] == name) return(g_zgbMapCode[i]);
   string src = InpGhAccountSalt + ":method:" + name;
   uchar data[], key[], res[];
   StringToCharArray(src, data, 0, StringLen(src), CP_UTF8);
   ArrayResize(key, 0);
   int v = 1;
   if(CryptEncode(CRYPT_HASH_SHA256, data, key, res) >= 2) v = ((int)res[0] * 256 + (int)res[1]) % 999 + 1;
   string code = StringFormat("M%03d", v);
   for(int k = 0; k < 999 && ZGB_CodeUsed(code); k++) { v = v % 999 + 1; code = StringFormat("M%03d", v); }
   int n = ArraySize(g_zgbMapName);
   ArrayResize(g_zgbMapName, n + 1);
   ArrayResize(g_zgbMapCode, n + 1);
   g_zgbMapName[n] = name;
   g_zgbMapCode[n] = code;
   if(StringLen(InpGhMethodMapFile) > 0)
     {
      int h = FileOpen(InpGhMethodMapFile, FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | ZGB_FileFlags());
      if(h != INVALID_HANDLE)
        {
         FileSeek(h, 0, SEEK_END);
         FileWriteString(h, code + "," + name + "\r\n");
         FileClose(h);
        }
     }
   return(code);
  }

// 2.5: the ONE comment -> code path. The aggregate report hashes the comment of each position's ENTRY deal, and the control
// channel must hit the same code, so the EA passes the order comment of the entry it is about to send (EA161 v1.32), and both
// sides call this function. A broker suffix ("[sl ...]", "[tp ...]": exit deals only, cut defensively) and anything past the
// 31 characters MT5 keeps of an order comment are removed before ZGB_MethodCode (trim, "," -> " ", salted hash, local map).
string ZGB_CodeForComment(const string cmt)
  {
   string s = cmt;
   int b = StringFind(s, "[");
   if(b >= 0) s = StringSubstr(s, 0, b);
   if(StringLen(s) > 31) s = StringSubstr(s, 0, 31);
   return(ZGB_MethodCode(s));
  }

// 2.5: phase label as the server keys it (sakura scripts/agg.py _phase_key: keep [A-Za-z0-9_-], max 24, empty -> "none"), so
// control.json "phases" taken from the reports' by_phase keys match the EA's label (EA161 passes "SD|PB|N" -> "SDPBN").
string ZGB_PhaseKey(const string phase)
  {
   string o = "";
   for(int i = 0; i < StringLen(phase) && StringLen(o) < 24; i++)
     {
      ushort ch = StringGetCharacter(phase, i);
      if((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '_' || ch == '-') o += ShortToString(ch);
     }
   return(StringLen(o) > 0 ? o : "none");
  }

// 2.7: trading session of a UTC time from the hour bands ZGB_SESS_*_H (= sakura scripts/agg.py SESSION_BANDS_UTC). A union of
// the month-to-date grid's 4-hour buckets. An additional report dimension: it never enters ZGB_PhaseKey or a control code.
string ZGB_SessionOf(const datetime t_utc)
  {
   MqlDateTime d;
   TimeToStruct(t_utc, d);
   if(d.hour >= ZGB_SESS_OFF_H) return("OFF");
   if(d.hour >= ZGB_SESS_NY_H)  return("NY");
   if(d.hour >= ZGB_SESS_LON_H) return("LON");
   return("ASIA");
  }

string ZGB_SessionNow(void) { return(ZGB_SessionOf(TimeGMT())); }   // 2.7: e.g. for the EA's shadow cells "SESS:REASON"

string ZGB_SideKey(const int side) { return(side > 0 ? "buy" : "sell"); }   // 2.7: by_side key (sakura agg.AGG_SIDES)

//--- NFP/CPI/FOMC times seen in latest.json are remembered locally for the news rule
void ZGB_EventsLoad(void)
  {
   ArrayResize(g_zgbEvT, 0);
   if(!FileIsExist(ZGB_EVENTS_FILE, ZGB_FileFlags())) return;
   int h = FileOpen(ZGB_EVENTS_FILE, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | ZGB_FileFlags());
   if(h == INVALID_HANDLE) return;
   while(!FileIsEnding(h))
     {
      long t = StringToInteger(ZGB_Trim(FileReadString(h)));
      if(t <= 0) continue;
      int n = ArraySize(g_zgbEvT);
      ArrayResize(g_zgbEvT, n + 1);
      g_zgbEvT[n] = t;
     }
   FileClose(h);
  }

void ZGB_EventsRemember(const string &s)
  {
   bool changed = false;
   string lists[2] = {"events.next.", "events.recent."};
   for(int l = 0; l < 2; l++)
      for(int k = 0; k < 8; k++)
        {
         double t;
         if(!ZGB_JNumber(s, lists[l] + IntegerToString(k) + ".t", t)) break;
         bool known = false;
         for(int i = 0; i < ArraySize(g_zgbEvT); i++) if(g_zgbEvT[i] == (long)t) { known = true; break; }
         if(known) continue;
         int n = ArraySize(g_zgbEvT);
         if(n >= 400) { for(int i = 1; i < n; i++) g_zgbEvT[i - 1] = g_zgbEvT[i]; n--; }
         ArrayResize(g_zgbEvT, n + 1);
         g_zgbEvT[n] = (long)t;
         changed = true;
        }
   if(!changed) return;
   string text = "";
   for(int i = 0; i < ArraySize(g_zgbEvT); i++) text += IntegerToString(g_zgbEvT[i]) + "\n";
   ZGB_WriteFile(ZGB_EVENTS_FILE, text);
  }

bool ZGB_InNewsBlock(const long t_utc)
  {
   long w = (long)MathMax(0, InpGhNewsBlockMin) * 60;
   for(int i = 0; i < ArraySize(g_zgbEvT); i++) if(MathAbs(t_utc - g_zgbEvT[i]) <= w) return(true);
   return(false);
  }

bool ZGB_Init(void)
  {
   g_zgbNetOk = false;
   if(!InpGhEnable) return(true);
   if(MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_OPTIMIZATION))
     {
      Print("ZumiGitHubBridge: Strategy Tester detected, WebRequest is not available there; bridge disabled.");
      return(true);
     }
   g_zgbNetOk = true;
   g_zgbAcct = ZGB_AccountHash();
   ZGB_LoadToken();
   ZGB_ReadFile(ZGB_CACHE_FILE, g_zgbLatest);
   ZGB_MapLoad();
   ZGB_EventsLoad();
   ZGB_PosxLoad();
   if(InpGhControlEnable)
     {
      string ctl, err;
      if(ZGB_ReadFile(ZGB_CTL_CACHE, ctl) && !ZGB_CtlApplyText(ctl, false, err)) ZGB_CtlClear();
      PrintFormat("ZumiGitHubBridge control: enabled, risk cap x%.2f, enable %s, news override %s",
                  ZGB_Clamp(InpGhMaxRiskMult, 0.0, ZGB_CTL_MAXMULT), InpGhAllowEnable ? "allowed" : "off",
                  InpGhAllowNewsOverride ? "allowed" : "off");
     }
   if(StringLen(g_zgbLatest) > 0) ZGB_EventsRemember(g_zgbLatest);
   if(InpGhReportMode != 0)
      Print("ZumiGitHubBridge: WARNING InpGhReportMode=1 sends every trade (times, prices, SL/TP). Use it ONLY with a PRIVATE repository.");
   if(GlobalVariableCheck(ZGB_GvName("dayend"))) g_zgbDayEndKey = (int)GlobalVariableGet(ZGB_GvName("dayend"));
   if(GlobalVariableCheck(ZGB_GvName("rslot"))) g_zgbRptSlot = (long)GlobalVariableGet(ZGB_GvName("rslot"));   // 2.4
   if(!GlobalVariableCheck(ZGB_GvName("ld_t")))
     {
      // first run on this account: report from the start of the current server day
      MqlDateTime t;
      TimeToStruct(TimeTradeServer(), t);
      t.hour = 0; t.min = 0; t.sec = 0;
      GlobalVariableSet(ZGB_GvName("ld_t"), (double)StructToTime(t));
      GlobalVariableSet(ZGB_GvName("ld_k"), 0.0);
     }
   if(InpGhAccountSalt == "change-me")
      Print("ZumiGitHubBridge: WARNING InpGhAccountSalt is the default; set your own private salt so the account hash cannot be reversed.");
   PrintFormat("ZumiGitHubBridge %s: repo %s/%s@%s, %s read, token %s, account hash %s, report mode %s",
               ZGB_VERSION, InpGhOwner, InpGhRepo, InpGhBranch, InpGhPrivateRepo ? "API" : "raw",
               StringLen(g_zgbToken) > 0 ? "set" : "NOT set (reports disabled)", g_zgbAcct,
               InpGhReportMode == 0 ? "aggregate" : "DETAIL (private repo only)");
   return(true);
  }

//+------------------------------------------------------------------+
//| READ: data/latest.json                                           |
//+------------------------------------------------------------------+
void ZGB_ResetLatest(ZgbLatest &o)
  {
   o.ok = false; o.reason = ""; o.generated = 0; o.age_h = EMPTY_VALUE;
   o.dgs10 = EMPTY_VALUE; o.dgs2 = EMPTY_VALUE; o.dfii10 = EMPTY_VALUE; o.t10yie = EMPTY_VALUE;
   o.usd = EMPTY_VALUE; o.vix = EMPTY_VALUE; o.oil = EMPTY_VALUE; o.spx = EMPTY_VALUE;
   o.vix_chg_pct = EMPTY_VALUE; o.dfii10_chg = EMPTY_VALUE; o.vix_date = "";
   o.vix_high = 0; o.vix_jump = 0;
   o.next_event = ""; o.next_event_utc = 0; o.next_event_unc = 0;
   o.gold_move_atr = EMPTY_VALUE;
  }

bool ZGB_FetchLatest(void)
  {
   string url, headers;
   if(InpGhPrivateRepo)
     {
      if(StringLen(g_zgbToken) == 0)
        {
         Print("ZumiGitHubBridge: private repo read needs a token (InpGhToken or token file)");
         g_zgbNextRead = TimeGMT() + 3600;
         return(false);
        }
      url = InpGhApiBase + "/repos/" + InpGhOwner + "/" + InpGhRepo + "/contents/" + InpGhLatestPath + "?ref=" + InpGhBranch;
      headers = ZGB_GhHeaders(true, "application/vnd.github.raw+json");
     }
   else
     {
      url = InpGhRawBase + "/" + InpGhOwner + "/" + InpGhRepo + "/" + InpGhBranch + "/" + InpGhLatestPath;
      headers = "User-Agent: zumi-ea-bridge\r\n";
     }
   string body;
   int code = ZGB_Http("GET", url, headers, "", body);
   if(code == -2) return(false);   // no budget / network disabled: try again next timer call
   datetime now = TimeGMT();
   string schema = "";
   if(code == 200 && ZGB_JString(body, "schema", schema) && schema == "zumi.latest")
     {
      g_zgbLatest = body;
      ZGB_WriteFile(ZGB_CACHE_FILE, body);
      ZGB_EventsRemember(g_zgbLatest);
      g_zgbReadFails = 0;
      g_zgbNextRead = now + (int)MathMax(1, InpGhReadEveryMin) * 60;
      return(true);
     }
   g_zgbReadFails++;
   int backoff = (int)MathMin(MathMax(1, InpGhReadEveryMin), 5 * MathPow(2, MathMin(g_zgbReadFails - 1, 4)));
   g_zgbNextRead = now + backoff * 60;
   if(code > 0) PrintFormat("ZumiGitHubBridge: latest.json HTTP %d (retry in %d min)", code, backoff);
   return(false);
  }

bool ZGB_ParseLatest(const string &s, ZgbLatest &o)
  {
   string schema = "";
   if(!ZGB_JString(s, "schema", schema) || schema != "zumi.latest") { o.reason = "parse"; return(false); }
   double gen = ZGB_JNumOr(s, "generated_unix", 0);
   if(gen <= 0) { o.reason = "parse"; return(false); }
   o.generated = (datetime)(long)gen;
   o.age_h = (double)(TimeGMT() - o.generated) / 3600.0;
   if(o.age_h > InpGhMaxAgeHours) { o.reason = "stale"; return(false); }
   o.dgs10  = ZGB_JNumOr(s, "series.DGS10.v", EMPTY_VALUE);
   o.dgs2   = ZGB_JNumOr(s, "series.DGS2.v", EMPTY_VALUE);
   o.dfii10 = ZGB_JNumOr(s, "series.DFII10.v", EMPTY_VALUE);
   o.dfii10_chg = ZGB_JNumOr(s, "series.DFII10.c1", EMPTY_VALUE);
   o.t10yie = ZGB_JNumOr(s, "series.T10YIE.v", EMPTY_VALUE);
   o.usd    = ZGB_JNumOr(s, "series.DTWEXBGS.v", EMPTY_VALUE);
   o.vix    = ZGB_JNumOr(s, "series.VIXCLS.v", EMPTY_VALUE);
   o.vix_chg_pct = ZGB_JNumOr(s, "series.VIXCLS.c1p", EMPTY_VALUE);
   ZGB_JString(s, "series.VIXCLS.d", o.vix_date);
   o.oil    = ZGB_JNumOr(s, "series.DCOILWTICO.v", EMPTY_VALUE);
   o.spx    = ZGB_JNumOr(s, "series.SP500.v", EMPTY_VALUE);
   o.vix_high = (int)ZGB_JNumOr(s, "flags.vix_high", 0);
   o.vix_jump = (int)ZGB_JNumOr(s, "flags.vix_jump", 0);
   o.gold_move_atr = ZGB_JNumOr(s, "gold.move_atr", EMPTY_VALUE);
   // first upcoming event that is still in the future for this terminal's clock
   long nowu = (long)TimeGMT();
   for(int k = 0; k < 8; k++)
     {
      double t;
      if(!ZGB_JNumber(s, "events.next." + IntegerToString(k) + ".t", t)) break;
      if((long)t <= nowu) continue;
      ZGB_JString(s, "events.next." + IntegerToString(k) + ".ev", o.next_event);
      o.next_event_utc = (datetime)(long)t;
      o.next_event_unc = (int)ZGB_JNumOr(s, "events.next." + IntegerToString(k) + ".unc", 0);
      break;
     }
   o.ok = true;
   return(true);
  }

// Fail-safe read: true only with fresh data. Network is used at most every InpGhReadEveryMin.
bool ZGB_ReadLatest(ZgbLatest &out, const bool allow_network = true)
  {
   ZGB_ResetLatest(out);
   if(!InpGhEnable) { out.reason = "disabled"; return(false); }
   if(MQLInfoInteger(MQL_TESTER)) { out.reason = "tester"; return(false); }
   if(allow_network && g_zgbNetOk && TimeGMT() >= g_zgbNextRead)
      ZGB_FetchLatest();
   if(StringLen(g_zgbLatest) == 0) { out.reason = "no data"; return(false); }
   return(ZGB_ParseLatest(g_zgbLatest, out));
  }

// Generic accessors on the cached latest.json, e.g. ZGB_GetNumber("series.T10YIE.c5", v)
bool ZGB_GetNumber(const string path, double &v)  { return(StringLen(g_zgbLatest) > 0 && ZGB_JNumber(g_zgbLatest, path, v)); }
bool ZGB_GetString(const string path, string &v)  { return(StringLen(g_zgbLatest) > 0 && ZGB_JString(g_zgbLatest, path, v)); }

//+------------------------------------------------------------------+
//| CONTROL CHANNEL: control/control.json (schema v1)                |
//+------------------------------------------------------------------+
// (control-channel #defines moved to the top of the file)

bool     g_zgbCtlValid   = false;   // a valid file is loaded (it may still be expired / stale at query time)
long     g_zgbCtlSeq     = 0;
long     g_zgbCtlUpdated = 0;       // unix UTC
datetime g_zgbCtlNextRead = 0;
bool     g_zgbCtlWasActive = false;
double   g_zgbCtlGMult   = 1.0;
bool     g_zgbCtlPauseAll = false;
double   g_zgbCtlORP     = 0.0;     // 2.3: global.open_risk_pct (0 = no remote value)
int      g_zgbCtlSide    = ZGB_SIDE_BOTH;   // 2.6: global.side (both = no restriction)
string ZGB_SideName(const int mode)          // 2.6: global.side value -> name (log / panel / mkt.ctx)
  {
   if(mode == ZGB_SIDE_BUY) return("buy");
   if(mode == ZGB_SIDE_SELL) return("sell");
   if(mode == ZGB_SIDE_AUTO) return("auto");
   return("both");
  }
long     g_zgbCtlGExp    = 0;
string   g_zgbCtlGReason = "";
string   g_zgbCtlCode[];
string   g_zgbCtlMode[];
double   g_zgbCtlMult[];
string   g_zgbCtlPhases[];          // ",A,B," ("" = every phase)
long     g_zgbCtlExp[];
string   g_zgbCtlReason[];
string   g_zgbCtlWKind[];
long     g_zgbCtlWStart[];
long     g_zgbCtlWEnd[];
string   g_zgbCtlWReason[];

// keys of the object at path ("methods"), in file order
int ZGB_JKeys(const string &s, const string path, string &keys[])
  {
   ArrayResize(keys, 0);
   int i = ZGB_JPath(s, path);
   int n = StringLen(s);
   if(i < 0 || i >= n || StringGetCharacter(s, i) != '{') return(0);
   i++;
   while(i < n)
     {
      i = ZGB_JWs(s, i);
      if(i >= n || StringGetCharacter(s, i) != '"') break;
      int ke = ZGB_JSkipStr(s, i);
      if(ke < 0) break;
      int k = ArraySize(keys);
      ArrayResize(keys, k + 1);
      keys[k] = StringSubstr(s, i + 1, ke - i - 2);
      i = ZGB_JWs(s, ke);
      if(i >= n || StringGetCharacter(s, i) != ':') break;
      i = ZGB_JSkipVal(s, i + 1);
      if(i < 0) break;
      i = ZGB_JWs(s, i);
      if(i < n && StringGetCharacter(s, i) == ',') { i++; continue; }
      break;
     }
   return(ArraySize(keys));
  }

long ZGB_IsoToUnix(const string iso)   // "2026-11-28T12:30:16Z" -> unix seconds (UTC)
  {
   if(StringLen(iso) < 19) return(0);
   string t = StringSubstr(iso, 0, 19);
   StringReplace(t, "-", ".");
   StringReplace(t, "T", " ");
   return((long)StringToTime(t));
  }

long ZGB_JTime(const string &s, const string base)   // reads base_unix, falls back to base_utc
  {
   double v;
   if(ZGB_JNumber(s, base + "_unix", v) && v > 0) return((long)v);
   string iso;
   if(ZGB_JString(s, base + "_utc", iso)) return(ZGB_IsoToUnix(iso));
   return(0);
  }

bool ZGB_IsMethodCode(const string c)
  {
   if(StringLen(c) != 4 || StringGetCharacter(c, 0) != 'M') return(false);
   for(int i = 1; i < 4; i++) { ushort ch = StringGetCharacter(c, i); if(ch < '0' || ch > '9') return(false); }
   return(true);
  }

bool ZGB_IsReason(const string c)
  {
   return(StringLen(c) == 3 && StringGetCharacter(c, 0) == 'R' && StringGetCharacter(c, 1) >= '0' &&
          StringGetCharacter(c, 1) <= '9' && StringGetCharacter(c, 2) >= '0' && StringGetCharacter(c, 2) <= '9');
  }

double ZGB_Clamp(const double v, const double lo, const double hi) { return(MathMax(lo, MathMin(hi, v))); }

void ZGB_CtlClear(void)
  {
   g_zgbCtlValid = false;
   g_zgbCtlGMult = 1.0; g_zgbCtlPauseAll = false; g_zgbCtlORP = 0.0; g_zgbCtlSide = ZGB_SIDE_BOTH; g_zgbCtlGExp = 0; g_zgbCtlGReason = "";
   ArrayResize(g_zgbCtlCode, 0); ArrayResize(g_zgbCtlMode, 0); ArrayResize(g_zgbCtlMult, 0);
   ArrayResize(g_zgbCtlPhases, 0); ArrayResize(g_zgbCtlExp, 0); ArrayResize(g_zgbCtlReason, 0);
   ArrayResize(g_zgbCtlWKind, 0); ArrayResize(g_zgbCtlWStart, 0); ArrayResize(g_zgbCtlWEnd, 0); ArrayResize(g_zgbCtlWReason, 0);
  }

// Full local validation + clamping. Returns false (with err) on any problem: the caller applies NEUTRAL.
bool ZGB_CtlApplyText(const string &s, const bool save, string &err)
  {
   err = "";
   double v, seq;
   string author;
   if(!ZGB_JNumber(s, "v", v) || (int)v != 1) { err = "version"; return(false); }
   if(!ZGB_JString(s, "author", author) || author != "claude") { err = "author"; return(false); }
   if(!ZGB_JNumber(s, "seq", seq) || seq < 1) { err = "seq"; return(false); }
   long upd = ZGB_JTime(s, "updated");
   long now = (long)TimeGMT();
   if(upd <= 0 || upd > now + 600) { err = "updated_utc"; return(false); }
   if(g_zgbCtlValid && (long)seq < g_zgbCtlSeq) { err = "older seq (ignored, keeping the current file)"; return(true); }
   if(g_zgbCtlValid && (long)seq == g_zgbCtlSeq && upd == g_zgbCtlUpdated) return(true);   // unchanged
   // ---- global
   double gm = 1.0, pa = 0;
   if(ZGB_JNumber(s, "global.risk_mult", v)) gm = ZGB_Clamp(v, 0.0, ZGB_CTL_MAXMULT);
   ZGB_JNumber(s, "global.pause_all", pa);
   double orp = 0.0;                                                  // 2.3: absent / invalid = no remote value
   if(ZGB_JNumber(s, "global.open_risk_pct", v))
     {
      if(MathIsValidNumber(v) && v >= ZGB_ORP_MIN && v <= ZGB_ORP_MAX) orp = v;
      else PrintFormat("ZumiGitHubBridge control: global.open_risk_pct %.4f outside [%.0f, %.0f] -> ignored (EA local cap)", v, ZGB_ORP_MIN, ZGB_ORP_MAX);
     }
   int gside = ZGB_SIDE_BOTH;                                         // 2.6: absent = both; an unknown value makes the file invalid (NEUTRAL)
   string gside_s = "";
   if(ZGB_JString(s, "global.side", gside_s))
     {
      if(gside_s == "both") gside = ZGB_SIDE_BOTH;
      else if(gside_s == "buy") gside = ZGB_SIDE_BUY;
      else if(gside_s == "sell") gside = ZGB_SIDE_SELL;
      else if(gside_s == "auto") gside = ZGB_SIDE_AUTO;
      else { err = "global.side"; return(false); }
     }
   long gexp = ZGB_JTime(s, "global.expires");
   if(gexp <= 0) gexp = upd + 48 * 3600;
   if(gexp <= upd) { err = "global.expires"; return(false); }
   if(gexp > upd + ZGB_CTL_MAX_TTL) gexp = upd + ZGB_CTL_MAX_TTL;
   string greason = "R00";
   ZGB_JString(s, "global.reason_code", greason);
   if(!ZGB_IsReason(greason)) { err = "global.reason_code"; return(false); }
   // ---- methods
   string codes[];
   int nm = ZGB_JKeys(s, "methods", codes);
   if(nm > 100) { err = "too many methods"; return(false); }
   string mmode[], mph[], mrs[];
   double mmul[];
   long mexp[];
   ArrayResize(mmode, nm); ArrayResize(mph, nm); ArrayResize(mrs, nm); ArrayResize(mmul, nm); ArrayResize(mexp, nm);
   for(int k = 0; k < nm; k++)
     {
      string b = "methods." + codes[k];
      if(!ZGB_IsMethodCode(codes[k])) { err = "method key is not a code"; return(false); }
      string mode_s = "";   // temporaries: array elements are not passed by reference
      if(!ZGB_JString(s, b + ".mode", mode_s)) { err = b + ".mode"; return(false); }
      mmode[k] = mode_s;
      double m = 1.0;
      ZGB_JNumber(s, b + ".risk_mult", m);
      m = ZGB_Clamp(m, 0.0, ZGB_CTL_MAXMULT);
      if(mmode[k] == "pause") m = 0.0;
      else if(mmode[k] == "on") m = 1.0;
      else if(mmode[k] == "reduce") { if(m >= 1.0) { err = b + " reduce needs < 1"; return(false); } }
      else if(mmode[k] == "boost") { if(m <= 1.0) { err = b + " boost needs > 1"; return(false); } }
      else { err = b + ".mode"; return(false); }
      mmul[k] = m;
      mexp[k] = ZGB_JTime(s, b + ".expires");
      if(mexp[k] <= 0) mexp[k] = upd + 48 * 3600;
      if(mexp[k] <= upd) { err = b + ".expires"; return(false); }
      if(mexp[k] > upd + ZGB_CTL_MAX_TTL) mexp[k] = upd + ZGB_CTL_MAX_TTL;
      string rc_s = "";
      if(!ZGB_JString(s, b + ".reason_code", rc_s) || !ZGB_IsReason(rc_s)) { err = b + ".reason_code"; return(false); }
      mrs[k] = rc_s;
      mph[k] = "";
      for(int q = 0; q < 10; q++)
        {
         string ph;
         if(!ZGB_JString(s, b + ".phases." + IntegerToString(q), ph)) break;
         mph[k] += (StringLen(mph[k]) == 0 ? "," : "") + ph + ",";
        }
     }
   // ---- news windows
   string wk[], wr[];
   long ws[], we[];
   int nw = 0;
   for(int q = 0; q < 51; q++)
     {
      string b = "news_blocks." + IntegerToString(q);
      string kind;
      if(!ZGB_JString(s, b + ".kind", kind)) break;
      if(q == 50) { err = "too many news windows"; return(false); }
      long st = ZGB_JTime(s, b + ".start"), en = ZGB_JTime(s, b + ".end");
      string rc = "";
      ZGB_JString(s, b + ".reason_code", rc);
      if((kind != "block" && kind != "allow") || st <= 0 || en <= st || !ZGB_IsReason(rc)) { err = b; return(false); }
      if(en - st > (kind == "allow" ? 4 * 3600 : 24 * 3600) || en > upd + ZGB_CTL_MAX_TTL) { err = b + " too long"; return(false); }
      ArrayResize(wk, nw + 1); ArrayResize(wr, nw + 1); ArrayResize(ws, nw + 1); ArrayResize(we, nw + 1);
      wk[nw] = kind; wr[nw] = rc; ws[nw] = st; we[nw] = en;
      nw++;
     }
   // ---- valid: log the differences with reason codes, then commit
   PrintFormat("ZumiGitHubBridge control: applying seq %I64d (updated %s UTC)", (long)seq, TimeToString((datetime)upd, TIME_DATE | TIME_MINUTES));
   if(orp != g_zgbCtlORP || !g_zgbCtlValid)
      PrintFormat("ZumiGitHubBridge control: global open_risk_pct %s until %s UTC (%s)%s", orp > 0 ? StringFormat("%.2f%%", orp) : "none (EA local cap)",
                  TimeToString((datetime)gexp, TIME_DATE | TIME_MINUTES), greason, (gexp <= (long)TimeGMT()) ? " (inactive: expired)" : "");
   if(gside != g_zgbCtlSide || !g_zgbCtlValid)
      PrintFormat("ZumiGitHubBridge control: global side %s until %s UTC (%s)%s", ZGB_SideName(gside),
                  TimeToString((datetime)gexp, TIME_DATE | TIME_MINUTES), greason, (gexp <= (long)TimeGMT()) ? " (inactive: expired)" : "");
   if(gm != g_zgbCtlGMult || (pa != 0) != g_zgbCtlPauseAll || !g_zgbCtlValid)
      PrintFormat("ZumiGitHubBridge control: global risk_mult %.2f (cap %.2f) pause_all %s until %s UTC (%s)", gm,
                  ZGB_Clamp(InpGhMaxRiskMult, 0.0, ZGB_CTL_MAXMULT), pa != 0 ? "true" : "false",
                  TimeToString((datetime)gexp, TIME_DATE | TIME_MINUTES), greason);
   for(int k = 0; k < nm; k++)
     {
      int old = -1;
      for(int q = 0; q < ArraySize(g_zgbCtlCode); q++) if(g_zgbCtlCode[q] == codes[k]) { old = q; break; }
      if(old < 0 || g_zgbCtlMode[old] != mmode[k] || g_zgbCtlMult[old] != mmul[k] || g_zgbCtlPhases[old] != mph[k] || g_zgbCtlExp[old] != mexp[k])
         PrintFormat("ZumiGitHubBridge control: %s %s x%.2f phases[%s] until %s UTC (%s)", codes[k], mmode[k], mmul[k],
                     mph[k], TimeToString((datetime)mexp[k], TIME_DATE | TIME_MINUTES), mrs[k]);
     }
   for(int q = 0; q < ArraySize(g_zgbCtlCode); q++)
     {
      bool kept = false;
      for(int k = 0; k < nm; k++) if(codes[k] == g_zgbCtlCode[q]) { kept = true; break; }
      if(!kept) PrintFormat("ZumiGitHubBridge control: %s directive removed (EA default)", g_zgbCtlCode[q]);
     }
   for(int q = 0; q < nw; q++)
      PrintFormat("ZumiGitHubBridge control: news %s %s .. %s UTC (%s)", wk[q], TimeToString((datetime)ws[q], TIME_DATE | TIME_MINUTES),
                  TimeToString((datetime)we[q], TIME_DATE | TIME_MINUTES), wr[q]);
   ZGB_CtlClear();
   g_zgbCtlSeq = (long)seq; g_zgbCtlUpdated = upd;
   g_zgbCtlGMult = gm; g_zgbCtlPauseAll = (pa != 0); g_zgbCtlORP = orp; g_zgbCtlSide = gside; g_zgbCtlGExp = gexp; g_zgbCtlGReason = greason;
   ArrayResize(g_zgbCtlCode, nm); ArrayResize(g_zgbCtlMode, nm); ArrayResize(g_zgbCtlMult, nm);
   ArrayResize(g_zgbCtlPhases, nm); ArrayResize(g_zgbCtlExp, nm); ArrayResize(g_zgbCtlReason, nm);
   for(int k = 0; k < nm; k++)
     {
      g_zgbCtlCode[k] = codes[k]; g_zgbCtlMode[k] = mmode[k]; g_zgbCtlMult[k] = mmul[k];
      g_zgbCtlPhases[k] = mph[k]; g_zgbCtlExp[k] = mexp[k]; g_zgbCtlReason[k] = mrs[k];
     }
   ArrayResize(g_zgbCtlWKind, nw); ArrayResize(g_zgbCtlWStart, nw); ArrayResize(g_zgbCtlWEnd, nw); ArrayResize(g_zgbCtlWReason, nw);
   for(int q = 0; q < nw; q++) { g_zgbCtlWKind[q] = wk[q]; g_zgbCtlWStart[q] = ws[q]; g_zgbCtlWEnd[q] = we[q]; g_zgbCtlWReason[q] = wr[q]; }
   g_zgbCtlValid = true;
   if(save) ZGB_WriteFile(ZGB_CTL_CACHE, s);
   return(true);
  }

// true while a valid, fresh control file is loaded and the channel is enabled (never in the tester)
bool ZGB_CtlActive(void)
  {
   if(!InpGhEnable || !InpGhControlEnable || MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_OPTIMIZATION)) return(false);
   if(!g_zgbCtlValid) return(false);
   return((long)TimeGMT() - g_zgbCtlUpdated <= ZGB_CTL_STALE_S);
  }

// Fetch (at most every InpGhControlEveryMin) and apply control.json. Returns true when directives are active.
bool ZGB_ReadControl(const bool allow_network = true)
  {
   if(!InpGhEnable || !InpGhControlEnable || MQLInfoInteger(MQL_TESTER)) return(false);
   datetime now = TimeGMT();
   if(allow_network && g_zgbNetOk && now >= g_zgbCtlNextRead)
     {
      string url, headers, body;
      if(InpGhPrivateRepo)
        {
         url = InpGhApiBase + "/repos/" + InpGhOwner + "/" + InpGhRepo + "/contents/" + InpGhControlPath + "?ref=" + InpGhBranch;
         headers = ZGB_GhHeaders(true, "application/vnd.github.raw+json");
        }
      else
        {
         url = InpGhRawBase + "/" + InpGhOwner + "/" + InpGhRepo + "/" + InpGhBranch + "/" + InpGhControlPath;
         headers = "User-Agent: zumi-ea-bridge\r\n";
        }
      int code = ZGB_Http("GET", url, headers, "", body);
      if(code != -2)
        {
         g_zgbCtlNextRead = now + (int)MathMax(5, InpGhControlEveryMin) * 60;
         if(code == 200)
           {
            string err;
            if(!ZGB_CtlApplyText(body, true, err))
              {
               if(g_zgbCtlValid || g_zgbCtlWasActive)
                  PrintFormat("ZumiGitHubBridge control: INVALID file (%s) -> NEUTRAL", err);
               ZGB_CtlClear();
              }
            else if(StringLen(err) > 0) Print("ZumiGitHubBridge control: " + err);
           }
         else if(code == 404)
           {
            if(g_zgbCtlValid) Print("ZumiGitHubBridge control: file missing (HTTP 404) -> NEUTRAL");
            ZGB_CtlClear();
           }
         else g_zgbCtlNextRead = now + 15 * 60;   // network problem: keep the last good file (it still expires)
        }
     }
   bool active = ZGB_CtlActive();
   if(g_zgbCtlWasActive && !active) Print("ZumiGitHubBridge control: no longer active (expired / stale / disabled) -> NEUTRAL");
   g_zgbCtlWasActive = active;
   return(active);
  }

int ZGB_CtlFind(const string code)
  {
   for(int i = 0; i < ArraySize(g_zgbCtlCode); i++) if(g_zgbCtlCode[i] == code) return(i);
   return(-1);
  }

// index of the method directive that applies now to (method, phase), or -1
int ZGB_CtlDirective(const string method_name, const string phase_label)
  {
   if(!ZGB_CtlActive()) return(-1);
   int k = ZGB_CtlFind(ZGB_CodeForComment(method_name));   // 2.5: method_name = the entry's order comment (= report code)
   if(k < 0 || (long)TimeGMT() >= g_zgbCtlExp[k]) return(-1);
   if(StringLen(g_zgbCtlPhases[k]) > 0 && StringFind(g_zgbCtlPhases[k], "," + ZGB_PhaseKey(phase_label) + ",") < 0) return(-1);
   return(k);
  }

bool ZGB_CtlGlobalActive(void) { return(ZGB_CtlActive() && (long)TimeGMT() < g_zgbCtlGExp); }

// 2.6: global.side in force now. NEUTRAL (channel off, tester, invalid / stale > 72 h file, global block expired) = both.
int ZGB_SideMode(void)
  {
   if(!ZGB_CtlGlobalActive()) return(ZGB_SIDE_BOTH);
   return(g_zgbCtlSide);
  }
// May a NEW entry on this side (+1 buy, -1 sell) be opened? Restrict only: true when NEUTRAL, both, or auto (auto defers to the
// EA's own confluence score: the EA checks ZGB_SideMode() == ZGB_SIDE_AUTO itself), or when the side matches.
bool ZGB_SideAllowed(const int side)
  {
   int m = ZGB_SideMode();
   if(m == ZGB_SIDE_BOTH || m == ZGB_SIDE_AUTO) return(true);
   if(side > 0) return(m == ZGB_SIDE_BUY);
   if(side < 0) return(m == ZGB_SIDE_SELL);
   return(true);
  }

// Risk multiplier for a method in a phase: global x method, clamped to [0, InpGhMaxRiskMult] (and never above 2.0).
double ZGB_RiskMult(const string method_name, const string phase_label)
  {
   if(!ZGB_CtlActive()) return(1.0);
   double m = 1.0;
   if(ZGB_CtlGlobalActive())
     {
      if(g_zgbCtlPauseAll) return(0.0);
      m = g_zgbCtlGMult;
     }
   int k = ZGB_CtlDirective(method_name, phase_label);
   if(k >= 0)
     {
      if(g_zgbCtlMode[k] == "pause") return(0.0);
      m *= g_zgbCtlMult[k];
     }
   double cap = ZGB_Clamp(InpGhMaxRiskMult, 0.0, ZGB_CTL_MAXMULT);
   return(ZGB_Clamp(m, 0.0, cap));
  }

// 2.3: total open-risk cap for new entries = min(local_cap, remote global.open_risk_pct) while the global block is active
// (valid, fresh <= 72 h, not expired, channel enabled, not the tester). Never above local_cap; never below 1.0 (unless local_cap < 1).
double ZGB_OpenRiskCap(const double local_cap)
  {
   double r = local_cap;
   if(ZGB_CtlGlobalActive() && g_zgbCtlORP >= ZGB_ORP_MIN) r = MathMin(local_cap, g_zgbCtlORP);
   r = MathMax(r, MathMin(ZGB_ORP_MIN, local_cap));
   return(MathMin(r, local_cap));
  }

// Base risk x multiplier, never above the EA's own max risk per trade (that cap is not overridable).
double ZGB_ApplyRisk(const double base_risk_pct, const double max_risk_pct, const string method_name, const string phase_label)
  {
   return(MathMin(base_risk_pct * ZGB_RiskMult(method_name, phase_label), max_risk_pct));
  }

bool ZGB_MethodPaused(const string method_name, const string phase_label)
  {
   if(!ZGB_CtlActive()) return(false);
   if(ZGB_CtlGlobalActive() && g_zgbCtlPauseAll) return(true);
   int k = ZGB_CtlDirective(method_name, phase_label);
   return(k >= 0 && g_zgbCtlMode[k] == "pause");
  }

// Should the method run? enabled_by_default = the EA's own setting. "on" can enable an off-by-default
// method only when InpGhAllowEnable is true. A pause always wins.
bool ZGB_MethodEnabled(const string method_name, const string phase_label, const bool enabled_by_default)
  {
   if(ZGB_MethodPaused(method_name, phase_label)) return(false);
   if(enabled_by_default) return(true);
   int k = ZGB_CtlDirective(method_name, phase_label);
   return(InpGhAllowEnable && k >= 0 && g_zgbCtlMode[k] == "on");
  }

// Extra news block from the control file. t_utc = TimeGMT()-based time. Can only ADD blocking.
bool ZGB_ExtraNewsBlock(const datetime t_utc)
  {
   if(!ZGB_CtlActive()) return(false);
   for(int i = 0; i < ArraySize(g_zgbCtlWKind); i++)
      if(g_zgbCtlWKind[i] == "block" && (long)t_utc >= g_zgbCtlWStart[i] && (long)t_utc < g_zgbCtlWEnd[i]) return(true);
   return(false);
  }

// "allow" window: may lift the EA's OWN news veto for that event, only with InpGhAllowNewsOverride.
// Never lifts the crisis layer or the loss stops; an extra block always wins over an allow.
bool ZGB_NewsAllowed(const datetime t_utc)
  {
   if(!ZGB_CtlActive() || !InpGhAllowNewsOverride || ZGB_ExtraNewsBlock(t_utc)) return(false);
   for(int i = 0; i < ArraySize(g_zgbCtlWKind); i++)
      if(g_zgbCtlWKind[i] == "allow" && (long)t_utc >= g_zgbCtlWStart[i] && (long)t_utc < g_zgbCtlWEnd[i]) return(true);
   return(false);
  }

//+------------------------------------------------------------------+
//| EA-side logging / flags / phase                                  |
//+------------------------------------------------------------------+
void ZGB_SetPhase(const string phase) { g_zgbPhase = StringSubstr(phase, 0, 48); }

// 2.4: market context for mkt.ctx (EA161 v1.31 calls it on each new M15 bar). Accepted only when it is one JSON object of
// at most ZGB_CTX_MAX bytes made of [A-Za-z0-9_:.,{}[]"-] (no spaces, no free text); otherwise the last good block is kept.
// The server (sakura scripts/agg.py) whitelists every key and range again.
void ZGB_SetMarketCtx(const string json_obj)
  {
   int n = StringLen(json_obj);
   bool ok = (n >= 2 && n <= ZGB_CTX_MAX && StringGetCharacter(json_obj, 0) == '{' && StringGetCharacter(json_obj, n - 1) == '}');
   int depth = 0, quotes = 0;
   for(int i = 0; ok && i < n; i++)
     {
      ushort ch = StringGetCharacter(json_obj, i);
      bool alnum = (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9');
      if(!alnum && ch != '_' && ch != ':' && ch != '.' && ch != ',' && ch != '-' && ch != '"' &&
         ch != '{' && ch != '}' && ch != '[' && ch != ']') ok = false;
      if(ch == '"') quotes++;
      if(ch == '{' || ch == '[') depth++;
      if(ch == '}' || ch == ']') { depth--; if(depth < 0) ok = false; }
     }
   if(ok && (depth != 0 || quotes % 2 != 0)) ok = false;
   if(!ok)
     {
      if(!g_zgbCtxWarned) PrintFormat("ZumiGitHubBridge: market context rejected (%d chars), keeping the previous one", n);
      g_zgbCtxWarned = true;
      return;
     }
   g_zgbCtx = json_obj;
   g_zgbCtxT = TimeGMT();
  }

// 2.4: the context is fresh while the EA keeps updating it (ticks flow); no heartbeat over a closed market.
bool ZGB_CtxFresh(const long slot_len)
  {
   return(StringLen(g_zgbCtx) > 0 && (long)TimeGMT() - (long)g_zgbCtxT <= slot_len + 1800);
  }

// 2.5: blocked-signal shadow statistics for data.shadow (EA161 v1.32 calls it on each new M15 bar and after changes). Same rule as the
// market context: one JSON object of at most ZGB_SHADOW_MAX bytes made of [A-Za-z0-9_:.,{}[]"-] (no spaces, no free text), otherwise the last
// good block is kept. Aggregate mode only; the server (sakura scripts/agg.py) whitelists every key, code and range again.
void ZGB_SetShadowStats(const string json_obj)
  {
   int n = StringLen(json_obj);
   bool ok = (n >= 2 && n <= ZGB_SHADOW_MAX && StringGetCharacter(json_obj, 0) == '{' && StringGetCharacter(json_obj, n - 1) == '}');
   int depth = 0, quotes = 0;
   for(int i = 0; ok && i < n; i++)
     {
      ushort ch = StringGetCharacter(json_obj, i);
      bool alnum = (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9');
      if(!alnum && ch != '_' && ch != ':' && ch != '.' && ch != ',' && ch != '-' && ch != '"' &&
         ch != '{' && ch != '}' && ch != '[' && ch != ']') ok = false;
      if(ch == '"') quotes++;
      if(ch == '{' || ch == '[') depth++;
      if(ch == '}' || ch == ']') { depth--; if(depth < 0) ok = false; }
     }
   if(ok && (depth != 0 || quotes % 2 != 0)) ok = false;
   if(!ok)
     {
      if(!g_zgbShadowWarned) PrintFormat("ZumiGitHubBridge: shadow statistics rejected (%d chars), keeping the previous ones", n);
      g_zgbShadowWarned = true;
      return;
     }
   g_zgbShadow = json_obj;
  }

void ZGB_Log(const string line)   // phase / analysis log line, sent with the next report
  {
   int n = ArraySize(g_zgbLogMsg);
   if(n >= ZGB_MAX_LOG)
     {
      for(int i = 1; i < n; i++) { g_zgbLogMsg[i - 1] = g_zgbLogMsg[i]; g_zgbLogT[i - 1] = g_zgbLogT[i]; }
      n--;
     }
   ArrayResize(g_zgbLogMsg, n + 1);
   ArrayResize(g_zgbLogT, n + 1);
   g_zgbLogMsg[n] = StringSubstr(line, 0, 300);
   g_zgbLogT[n] = TimeGMT();
  }

void ZGB_Push(string &q[], const string text)
  {
   int n = ArraySize(q);
   if(n >= 20) return;
   ArrayResize(q, n + 1);
   q[n] = StringSubstr(text, 0, 1800);
  }

void ZGB_Pop(string &q[])
  {
   int n = ArraySize(q);
   for(int i = 1; i < n; i++) q[i - 1] = q[i];
   if(n > 0) ArrayResize(q, n - 1);
  }

void ZGB_Notify(const string text)   // optional Telegram / Discord (sent from ZGB_OnTimer)
  {
   if(InpTgEnable && StringLen(InpTgBotToken) > 0 && StringLen(InpTgChatId) > 0) ZGB_Push(g_zgbTgQ, text);
   if(InpDcEnable && StringLen(InpDcWebhookUrl) > 0) ZGB_Push(g_zgbDcQ, text);
  }

// An EA-raised alert: goes into the next report (-> alerts PR comment) and to the instant channels.
void ZGB_Flag(const string key, const string msg)
  {
   int n = ArraySize(g_zgbFlagJson);
   if(n >= ZGB_MAX_FLAGS) return;
   ArrayResize(g_zgbFlagJson, n + 1);
   if(InpGhReportMode == 0)   // published: no timestamp, short message (do not put prices in it)
      g_zgbFlagJson[n] = StringFormat("{\"key\":\"%s\",\"msg\":\"%s\"}", ZGB_Esc(StringSubstr(key, 0, 40)),
                                      ZGB_Esc(StringSubstr(msg, 0, 150)));
   else
      g_zgbFlagJson[n] = StringFormat("{\"key\":\"%s\",\"msg\":\"%s\",\"t\":%I64d}", ZGB_Esc(StringSubstr(key, 0, 40)),
                                      ZGB_Esc(StringSubstr(msg, 0, 280)), (long)TimeGMT());
   ZGB_Notify("Zumi " + g_zgbAcct + ": " + msg);
  }

//+------------------------------------------------------------------+
//| MAE / MFE tracker                                                |
//+------------------------------------------------------------------+
int ZGB_TrkFind(const long pos)
  {
   for(int i = ArraySize(g_zgbTrkPos) - 1; i >= 0; i--) if(g_zgbTrkPos[i] == pos) return(i);
   return(-1);
  }

int ZGB_TrkAdd(const long pos)
  {
   int i = ZGB_TrkFind(pos);
   if(i >= 0) return(i);
   int n = ArraySize(g_zgbTrkPos);
   if(n >= ZGB_MAX_TRACK) return(-1);
   ArrayResize(g_zgbTrkPos, n + 1); ArrayResize(g_zgbTrkMin, n + 1);
   ArrayResize(g_zgbTrkMax, n + 1); ArrayResize(g_zgbTrkPhase, n + 1);
   g_zgbTrkPos[n] = pos; g_zgbTrkMin[n] = DBL_MAX; g_zgbTrkMax[n] = -DBL_MAX; g_zgbTrkPhase[n] = g_zgbPhase;
   return(n);
  }

void ZGB_TrkRemove(const long pos)
  {
   int i = ZGB_TrkFind(pos);
   if(i < 0) return;
   int last = ArraySize(g_zgbTrkPos) - 1;
   g_zgbTrkPos[i] = g_zgbTrkPos[last]; g_zgbTrkMin[i] = g_zgbTrkMin[last];
   g_zgbTrkMax[i] = g_zgbTrkMax[last]; g_zgbTrkPhase[i] = g_zgbTrkPhase[last];
   ArrayResize(g_zgbTrkPos, last); ArrayResize(g_zgbTrkMin, last);
   ArrayResize(g_zgbTrkMax, last); ArrayResize(g_zgbTrkPhase, last);
  }

// 2.2: the EA declares its own magic span so reports never count other EAs or manual trades (magic 0); no new input.
void ZGB_SetMagicRange(long lo, long hi)
  {
   if(lo < 0 || hi < lo) { g_zgbMagicLo = -1; g_zgbMagicHi = -1; return; }
   g_zgbMagicLo = lo; g_zgbMagicHi = hi;
  }

bool ZGB_PassFilter(const string sym, const long magic)
  {
   if(g_zgbMagicLo >= 0 && (magic < g_zgbMagicLo || magic > g_zgbMagicHi)) return(false);
   if(InpGhMagicFilter >= 0 && magic != InpGhMagicFilter) return(false);
   if(StringLen(InpGhSymbolFilter) > 0 && sym != InpGhSymbolFilter) return(false);
   return(true);
  }

// Call from OnTick (cheap, throttled to once per second): tracks min/max price of open positions.
void ZGB_OnTick(void)
  {
   if(!InpGhEnable || !g_zgbNetOk) return;
   datetime now = TimeCurrent();
   if(now == g_zgbLastTrack) return;
   g_zgbLastTrack = now;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      if(!ZGB_PassFilter(sym, PositionGetInteger(POSITION_MAGIC))) continue;
      int k = ZGB_TrkAdd(PositionGetInteger(POSITION_IDENTIFIER));
      if(k < 0) continue;
      bool buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      double px = buy ? SymbolInfoDouble(sym, SYMBOL_BID) : SymbolInfoDouble(sym, SYMBOL_ASK);
      if(px <= 0) continue;
      if(px < g_zgbTrkMin[k]) g_zgbTrkMin[k] = px;
      if(px > g_zgbTrkMax[k]) g_zgbTrkMax[k] = px;
     }
  }

// Call from OnTradeTransaction: closing deals mark a report as due (batched by ZGB_OnTimer).
void ZGB_OnTradeTransaction(const MqlTradeTransaction &trans)
  {
   if(!InpGhEnable || trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if(HistoryDealSelect(trans.deal))
     {
      long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN)
        {
         ZGB_TrkAdd(HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID));   // remember the phase at entry
         return;
        }
     }
   g_zgbDirty = true;
  }

//+------------------------------------------------------------------+
//| WRITE: build reports                                             |
//+------------------------------------------------------------------+
string ZGB_ReasonStr(const long r)
  {
   switch((int)r)
     {
      case DEAL_REASON_SL:     return("sl");
      case DEAL_REASON_TP:     return("tp");
      case DEAL_REASON_SO:     return("so");
      case DEAL_REASON_EXPERT: return("expert");
      case DEAL_REASON_CLIENT: return("client");
      case DEAL_REASON_MOBILE: return("mobile");
      case DEAL_REASON_WEB:    return("web");
     }
   return("other");
  }

double ZGB_DealNet(const ulong d)
  {
   return(HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP) +
          HistoryDealGetDouble(d, DEAL_COMMISSION) + HistoryDealGetDouble(d, DEAL_FEE));
  }

bool ZGB_IsTradeDeal(const ulong d)
  {
   long t = HistoryDealGetInteger(d, DEAL_TYPE);
   return(t == DEAL_TYPE_BUY || t == DEAL_TYPE_SELL);
  }

// Equity block in % only. day start = server midnight.
string ZGB_EquityJson(const bool with_margin = true)
  {
   MqlDateTime t;
   TimeToStruct(TimeTradeServer(), t);
   t.hour = 0; t.min = 0; t.sec = 0;
   datetime day0 = StructToTime(t);
   double trade_net = 0, other_net = 0;
   if(HistorySelect(day0, TimeTradeServer() + 60))
     {
      for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
        {
         ulong d = HistoryDealGetTicket(i);
         if(d == 0) continue;
         if(ZGB_IsTradeDeal(d)) trade_net += ZGB_DealNet(d);
         else other_net += ZGB_DealNet(d);   // deposits / withdrawals / credit
        }
     }
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   double b0  = bal - trade_net - other_net;
   if(b0 <= 0) b0 = bal;
   double day_pl = (b0 > 0) ? trade_net / b0 * 100.0 : EMPTY_VALUE;
   double eq_day = (b0 > 0) ? (eq - b0 - other_net) / b0 * 100.0 : EMPTY_VALUE;
   double flt    = (bal > 0) ? (eq - bal) / bal * 100.0 : EMPTY_VALUE;
   if(!with_margin)
      return(StringFormat("{\"day_pl_pct\":%s,\"eq_day_pct\":%s,\"float_pct\":%s}",
                          ZGB_Num(day_pl, 3), ZGB_Num(eq_day, 3), ZGB_Num(flt, 3)));
   return(StringFormat("{\"day_pl_pct\":%s,\"eq_day_pct\":%s,\"float_pct\":%s,\"margin_level\":%s}",
                       ZGB_Num(day_pl, 3), ZGB_Num(eq_day, 3), ZGB_Num(flt, 3),
                       ZGB_Num(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 1)));
  }

string ZGB_MarketJson(const bool full = true, const bool with_ctx = true)
  {
   string ctx = (with_ctx && StringLen(g_zgbCtx) > 0) ? ",\"ctx\":" + g_zgbCtx : "";   // 2.4
   string sym = (StringLen(InpGhMarketSymbol) > 0) ? InpGhMarketSymbol : _Symbol;
   int dg = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   double pc  = iClose(sym, PERIOD_D1, 1);
   double op  = iOpen(sym, PERIOD_D1, 0);
   double sum = 0;
   int n = 0;
   for(int i = 1; i <= 14; i++)
     {
      double h = iHigh(sym, PERIOD_D1, i), l = iLow(sym, PERIOD_D1, i), c = iClose(sym, PERIOD_D1, i + 1);
      if(h <= 0 || l <= 0 || c <= 0) break;
      sum += MathMax(h - l, MathMax(MathAbs(h - c), MathAbs(l - c)));
      n++;
     }
   double atr = (n == 14) ? sum / 14.0 : EMPTY_VALUE;
   double mv  = (bid > 0 && pc > 0) ? bid - pc : EMPTY_VALUE;
   double mva = (mv != EMPTY_VALUE && atr != EMPTY_VALUE && atr > 0) ? mv / atr : EMPTY_VALUE;
   if(!full)   // aggregate mode: no prices, only the normalised move
      return(StringFormat("{\"sym\":\"%s\",\"move_atr\":%s%s}", ZGB_Esc(sym), ZGB_Num(mva, 2), ctx));
   return(StringFormat("{\"sym\":\"%s\",\"bid\":%s,\"pc\":%s,\"d1_open\":%s,\"atr\":%s,\"move\":%s,\"move_atr\":%s,\"spread\":%s%s}",
                       ZGB_Esc(sym), ZGB_Num(bid, dg), ZGB_Num(pc, dg), ZGB_Num(op, dg), ZGB_Num(atr, dg),
                       ZGB_Num(mv, dg), ZGB_Num(mva, 3), ZGB_Num((ask > 0 && bid > 0) ? ask - bid : EMPTY_VALUE, dg), ctx));
  }

// Collects closing deals with ticket > last reported ticket. Returns the trade JSON objects.
int ZGB_CollectTrades(string &items[], ulong &max_ticket, datetime &max_time)
  {
   ArrayResize(items, 0);
   ulong    last_k = (ulong)GlobalVariableGet(ZGB_GvName("ld_k"));
   datetime last_t = (datetime)(long)GlobalVariableGet(ZGB_GvName("ld_t"));
   max_ticket = last_k;
   max_time = last_t;
   if(!HistorySelect(last_t - 60, TimeTradeServer() + 86400)) return(0);
   int total = HistoryDealsTotal();
   // balance just before each deal = balance now - sum of all later deals (suffix sum)
   double nets[];
   ulong  tks[];
   ArrayResize(nets, total);
   ArrayResize(tks, total);
   for(int i = 0; i < total; i++) { tks[i] = HistoryDealGetTicket(i); nets[i] = (tks[i] > 0) ? ZGB_DealNet(tks[i]) : 0; }
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double base[];
   ArrayResize(base, total);
   double suffix = 0;
   for(int i = total - 1; i >= 0; i--) { suffix += nets[i]; base[i] = bal - suffix; }

   ulong  exit_t[];
   long   exit_p[];
   double exit_base[];
   int ne = 0;
   for(int i = 0; i < total; i++)
     {
      ulong d = tks[i];
      if(d == 0 || d <= last_k || !ZGB_IsTradeDeal(d)) continue;
      long entry = HistoryDealGetInteger(d, DEAL_ENTRY);
      datetime dtm = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
      if(d > max_ticket) { max_ticket = d; }
      if(dtm > max_time) { max_time = dtm; }
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY && entry != DEAL_ENTRY_INOUT) continue;
      if(!ZGB_PassFilter(HistoryDealGetString(d, DEAL_SYMBOL), HistoryDealGetInteger(d, DEAL_MAGIC))) continue;
      ArrayResize(exit_t, ne + 1); ArrayResize(exit_p, ne + 1); ArrayResize(exit_base, ne + 1);
      exit_t[ne] = d;
      exit_p[ne] = HistoryDealGetInteger(d, DEAL_POSITION_ID);
      exit_base[ne] = base[i];
      ne++;
     }

   for(int j = 0; j < ne; j++)
     {
      ulong d = exit_t[j];
      if(!HistorySelectByPosition(exit_p[j])) continue;   // NOTE: replaces the history selection
      // entry context of the position
      double entry_px = 0, sl = 0, tp = 0, vol_in = 0, entry_comm = 0;
      long   magic = 0, etype = -1;
      ulong  entry_order = 0;
      datetime t_in = 0;
      string cmt = "";
      int nd = HistoryDealsTotal();
      for(int k = 0; k < nd; k++)
        {
         ulong e = HistoryDealGetTicket(k);
         if(e == 0 || HistoryDealGetInteger(e, DEAL_ENTRY) != DEAL_ENTRY_IN) continue;
         double v = HistoryDealGetDouble(e, DEAL_VOLUME);
         if(t_in == 0)
           {
            t_in = (datetime)HistoryDealGetInteger(e, DEAL_TIME);
            entry_px = HistoryDealGetDouble(e, DEAL_PRICE);
            sl = HistoryDealGetDouble(e, DEAL_SL);   // SL of the opening order = initial risk
            tp = HistoryDealGetDouble(e, DEAL_TP);
            magic = HistoryDealGetInteger(e, DEAL_MAGIC);
            etype = HistoryDealGetInteger(e, DEAL_TYPE);
            cmt = HistoryDealGetString(e, DEAL_COMMENT);
            entry_order = (ulong)HistoryDealGetInteger(e, DEAL_ORDER);
           }
         vol_in += v;
         entry_comm += HistoryDealGetDouble(e, DEAL_COMMISSION) + HistoryDealGetDouble(e, DEAL_FEE);
        }
      // fall back to the SL of the opening order (after the loop: selecting changes the cache)
      if(sl == 0 && entry_order > 0 && HistoryOrderSelect(entry_order))
         sl = HistoryOrderGetDouble(entry_order, ORDER_SL);
      if(!HistoryDealSelect(d)) continue;
      string sym = HistoryDealGetString(d, DEAL_SYMBOL);
      int dg = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      if(dg <= 0) dg = 5;
      double vol = HistoryDealGetDouble(d, DEAL_VOLUME);
      double out_px = HistoryDealGetDouble(d, DEAL_PRICE);
      bool buy = (etype >= 0) ? (etype == DEAL_TYPE_BUY) : (HistoryDealGetInteger(d, DEAL_TYPE) == DEAL_TYPE_SELL);
      if(t_in == 0) { entry_px = out_px; vol_in = vol; }   // entry not in history (very old position)
      double net = ZGB_DealNet(d) + ((vol_in > 0) ? entry_comm * vol / vol_in : 0);
      double b = exit_base[j];
      double pl_pct = (b > 0) ? net / b * 100.0 : EMPTY_VALUE;
      double risk = (sl > 0) ? MathAbs(entry_px - sl) : 0;
      double r = (risk > 0) ? (buy ? out_px - entry_px : entry_px - out_px) / risk : EMPTY_VALUE;
      double risk_pct = EMPTY_VALUE, loss = 0;
      if(sl > 0 && vol_in > 0 && b > 0 &&
         OrderCalcProfit(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, sym, vol_in, entry_px, sl, loss))
         risk_pct = MathAbs(loss) / b * 100.0;
      double mae = EMPTY_VALUE, mfe = EMPTY_VALUE;
      string phase = g_zgbPhase;
      int k = ZGB_TrkFind(exit_p[j]);
      if(k >= 0)
        {
         phase = g_zgbTrkPhase[k];
         if(risk > 0 && g_zgbTrkMin[k] < DBL_MAX)
           {
            mae = buy ? (g_zgbTrkMin[k] - entry_px) / risk : (entry_px - g_zgbTrkMax[k]) / risk;
            mfe = buy ? (g_zgbTrkMax[k] - entry_px) / risk : (entry_px - g_zgbTrkMin[k]) / risk;
            if(mae > 0) mae = 0;
           }
        }
      string item = StringFormat(
         "{\"deal\":%I64u,\"pos\":%I64d,\"sym\":\"%s\",\"side\":\"%s\",\"vol\":%s,\"vol_in\":%s,"
         "\"t_in\":%I64d,\"t_out\":%I64d,\"in\":%s,\"out\":%s,\"sl\":%s,\"tp\":%s,\"r\":%s,"
         "\"pl_pct\":%s,\"risk_pct\":%s,\"cmt\":\"%s\",\"magic\":%I64d,\"phase\":\"%s\",\"reason\":\"%s\","
         "\"mae_r\":%s,\"mfe_r\":%s}",
         d, exit_p[j], ZGB_Esc(sym), buy ? "buy" : "sell", ZGB_Num(vol, 2), ZGB_Num(vol_in, 2),
         (t_in > 0) ? ZGB_ToUtc(t_in) : (long)0, ZGB_ToUtc((datetime)HistoryDealGetInteger(d, DEAL_TIME)),
         ZGB_Num(entry_px, dg), ZGB_Num(out_px, dg), ZGB_Num(sl, dg), ZGB_Num(tp, dg), ZGB_Num(r, 3),
         ZGB_Num(pl_pct, 4), ZGB_Num(risk_pct, 3), ZGB_Esc(StringSubstr(cmt, 0, 60)), magic,
         ZGB_Esc(phase), ZGB_ReasonStr(HistoryDealGetInteger(d, DEAL_REASON)), ZGB_Num(mae, 3), ZGB_Num(mfe, 3));
      int n = ArraySize(items);
      ArrayResize(items, n + 1);
      items[n] = item;
      if(!PositionSelectByTicket((ulong)exit_p[j])) ZGB_TrkRemove(exit_p[j]);   // fully closed
     }
   return(ArraySize(items));
  }

//+------------------------------------------------------------------+
//| AGGREGATE MODE: closed positions -> per-period statistics        |
//+------------------------------------------------------------------+
//--- phase / MAE / MFE of closed positions, kept in a LOCAL file so month-to-date stats stay stable
//--- across restarts (line: id,close_unix,phase,has_ex,mae,mfe). Never published as such.
long     g_zgbXId[];
long     g_zgbXT[];
string   g_zgbXPh[];
bool     g_zgbXHas[];
double   g_zgbXMae[];
double   g_zgbXMfe[];
#define ZGB_POSX_FILE "zumi_gh_posx.csv"

void ZGB_PosxLoad(void)
  {
   ArrayResize(g_zgbXId, 0); ArrayResize(g_zgbXT, 0); ArrayResize(g_zgbXPh, 0);
   ArrayResize(g_zgbXHas, 0); ArrayResize(g_zgbXMae, 0); ArrayResize(g_zgbXMfe, 0);
   if(!FileIsExist(ZGB_POSX_FILE, ZGB_FileFlags())) return;
   int h = FileOpen(ZGB_POSX_FILE, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ | ZGB_FileFlags());
   if(h == INVALID_HANDLE) return;
   while(!FileIsEnding(h))
     {
      string f[];
      if(StringSplit(ZGB_Trim(FileReadString(h)), ',', f) != 6) continue;
      int n = ArraySize(g_zgbXId);
      ArrayResize(g_zgbXId, n + 1); ArrayResize(g_zgbXT, n + 1); ArrayResize(g_zgbXPh, n + 1);
      ArrayResize(g_zgbXHas, n + 1); ArrayResize(g_zgbXMae, n + 1); ArrayResize(g_zgbXMfe, n + 1);
      g_zgbXId[n] = StringToInteger(f[0]);
      g_zgbXT[n] = StringToInteger(f[1]);
      g_zgbXPh[n] = f[2];
      g_zgbXHas[n] = (f[3] == "1");
      g_zgbXMae[n] = StringToDouble(f[4]);
      g_zgbXMfe[n] = StringToDouble(f[5]);
     }
   FileClose(h);
  }

void ZGB_PosxSave(void)
  {
   long cutoff = (long)TimeGMT() - 45 * 86400;   // month to date + rolling week never need more
   string text = "";
   for(int i = 0; i < ArraySize(g_zgbXId); i++)
      if(g_zgbXT[i] >= cutoff)
         text += StringFormat("%I64d,%I64d,%s,%d,%.3f,%.3f\n", g_zgbXId[i], g_zgbXT[i], g_zgbXPh[i],
                              g_zgbXHas[i] ? 1 : 0, g_zgbXMae[i], g_zgbXMfe[i]);
   ZGB_WriteFile(ZGB_POSX_FILE, text);
  }

int ZGB_PosxFind(const long id)
  {
   for(int i = ArraySize(g_zgbXId) - 1; i >= 0; i--) if(g_zgbXId[i] == id) return(i);
   return(-1);
  }

void ZGB_PosxAdd(const long id, const long t_close, const string phase, const bool has, const double mae, const double mfe)
  {
   int n = ArraySize(g_zgbXId);
   ArrayResize(g_zgbXId, n + 1); ArrayResize(g_zgbXT, n + 1); ArrayResize(g_zgbXPh, n + 1);
   ArrayResize(g_zgbXHas, n + 1); ArrayResize(g_zgbXMae, n + 1); ArrayResize(g_zgbXMfe, n + 1);
   string ph = phase;
   StringReplace(ph, ",", " ");
   g_zgbXId[n] = id; g_zgbXT[n] = t_close; g_zgbXPh[n] = ph;
   g_zgbXHas[n] = has; g_zgbXMae[n] = mae; g_zgbXMfe[n] = mfe;
  }

// Loads every position whose exits fall in [from, now] and that is fully closed.
int ZGB_LoadPositions(const datetime from, ZgbPos &ps[])
  {
   ArrayResize(ps, 0);
   if(!HistorySelect(from, TimeTradeServer() + 86400)) return(0);
   int total = HistoryDealsTotal();
   ulong  tks[];
   double nets[], base[];
   ArrayResize(tks, total); ArrayResize(nets, total); ArrayResize(base, total);
   for(int i = 0; i < total; i++) { tks[i] = HistoryDealGetTicket(i); nets[i] = (tks[i] > 0) ? ZGB_DealNet(tks[i]) : 0; }
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double suffix = 0;
   for(int i = total - 1; i >= 0; i--) { suffix += nets[i]; base[i] = bal - suffix; }
   ulong  xt[];
   double xb[];
   long   pids[];
   int nx = 0, np = 0;
   bool posx_changed = false;
   for(int i = 0; i < total; i++)
     {
      ulong d = tks[i];
      if(d == 0 || !ZGB_IsTradeDeal(d)) continue;
      long entry = HistoryDealGetInteger(d, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY && entry != DEAL_ENTRY_INOUT) continue;
      if(!ZGB_PassFilter(HistoryDealGetString(d, DEAL_SYMBOL), HistoryDealGetInteger(d, DEAL_MAGIC))) continue;
      ArrayResize(xt, nx + 1); ArrayResize(xb, nx + 1);
      xt[nx] = d; xb[nx] = base[i]; nx++;
      long pid = HistoryDealGetInteger(d, DEAL_POSITION_ID);
      bool seen = false;
      for(int k = 0; k < np; k++) if(pids[k] == pid) { seen = true; break; }
      if(!seen) { ArrayResize(pids, np + 1); pids[np++] = pid; }
     }
   for(int j = 0; j < np; j++)
     {
      if(PositionSelectByTicket((ulong)pids[j])) continue;    // still open: counted once closed
      if(!HistorySelectByPosition(pids[j])) continue;          // NOTE: replaces the history selection
      double entry_px = 0, sl = 0, vol_in = 0, entry_comm = 0, vol_out = 0;
      long   etype = -1;
      ulong  entry_order = 0;
      datetime t_in = 0, t_out = 0;
      string cmt = "";
      double ex_px[], ex_vol[], ex_net[];
      ulong  ex_tk[];
      int ne = 0;
      int nd = HistoryDealsTotal();
      for(int k = 0; k < nd; k++)
        {
         ulong e = HistoryDealGetTicket(k);
         if(e == 0 || !ZGB_IsTradeDeal(e)) continue;
         long en = HistoryDealGetInteger(e, DEAL_ENTRY);
         if(en == DEAL_ENTRY_IN)
           {
            if(t_in == 0)
              {
               t_in = (datetime)HistoryDealGetInteger(e, DEAL_TIME);
               entry_px = HistoryDealGetDouble(e, DEAL_PRICE);
               sl = HistoryDealGetDouble(e, DEAL_SL);
               etype = HistoryDealGetInteger(e, DEAL_TYPE);
               cmt = HistoryDealGetString(e, DEAL_COMMENT);
               entry_order = (ulong)HistoryDealGetInteger(e, DEAL_ORDER);
              }
            vol_in += HistoryDealGetDouble(e, DEAL_VOLUME);
            entry_comm += HistoryDealGetDouble(e, DEAL_COMMISSION) + HistoryDealGetDouble(e, DEAL_FEE);
           }
         else
           {
            ArrayResize(ex_px, ne + 1); ArrayResize(ex_vol, ne + 1); ArrayResize(ex_net, ne + 1); ArrayResize(ex_tk, ne + 1);
            ex_px[ne] = HistoryDealGetDouble(e, DEAL_PRICE);
            ex_vol[ne] = HistoryDealGetDouble(e, DEAL_VOLUME);
            ex_net[ne] = ZGB_DealNet(e);
            ex_tk[ne] = e;
            datetime te = (datetime)HistoryDealGetInteger(e, DEAL_TIME);
            if(te > t_out) t_out = te;
            vol_out += ex_vol[ne];
            ne++;
           }
        }
      if(ne == 0 || t_in == 0 || vol_in <= 0 || vol_out < vol_in - 1e-8) continue;
      if(sl == 0 && entry_order > 0 && HistoryOrderSelect(entry_order))
         sl = HistoryOrderGetDouble(entry_order, ORDER_SL);
      bool buy = (etype == DEAL_TYPE_BUY);
      string sym = HistoryDealGetString(ex_tk[0], DEAL_SYMBOL);
      double risk = (sl > 0) ? MathAbs(entry_px - sl) : 0;
      double r = 0, pl = 0, last_base = bal;
      for(int k = 0; k < ne; k++)
        {
         double b = last_base;
         for(int q = 0; q < nx; q++) if(xt[q] == ex_tk[k]) { b = xb[q]; break; }
         last_base = b;
         double net = ex_net[k] + entry_comm * ex_vol[k] / vol_in;
         if(b > 0) pl += net / b * 100.0;
         if(risk > 0) r += (buy ? ex_px[k] - entry_px : entry_px - ex_px[k]) / risk * ex_vol[k] / vol_in;
        }
      int n = ArraySize(ps);
      ArrayResize(ps, n + 1);
      ps[n].id = pids[j];
      ps[n].t_in = t_in;
      ps[n].t_out = t_out;
      ps[n].r = r;
      ps[n].has_r = (risk > 0);
      ps[n].pl = pl;
      ps[n].mcode = ZGB_CodeForComment(cmt);   // entry deal comment (2.5: same path as the control channel)
      ps[n].side = buy ? 1 : -1;                                    // 2.7: by_side
      ps[n].sess = ZGB_SessionOf((datetime)ZGB_ToUtc(t_in));        // 2.7: by_session (entry time, UTC)
      ps[n].no_sl = (sl <= 0);
      double loss = 0;
      ps[n].risk_over = (sl > 0 && last_base > 0 &&
                         OrderCalcProfit(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, sym, vol_in, entry_px, sl, loss) &&
                         MathAbs(loss) / last_base * 100.0 > InpGhRiskCapPct + 1e-9);
      ps[n].news = ZGB_InNewsBlock(ZGB_ToUtc(t_in));
      ps[n].phase = "none";
      ps[n].has_ex = false;
      ps[n].mae = 0;
      ps[n].mfe = 0;
      int xi = ZGB_PosxFind(pids[j]);
      int tk = ZGB_TrkFind(pids[j]);
      if(xi < 0 && tk >= 0)
        {
         // first time this closed position is aggregated: move its tracker data to the local file
         string ph = (StringLen(g_zgbTrkPhase[tk]) > 0) ? g_zgbTrkPhase[tk] : "none";
         bool has = (risk > 0 && g_zgbTrkMin[tk] < DBL_MAX);
         double mae = has ? MathMin(0, buy ? (g_zgbTrkMin[tk] - entry_px) / risk : (entry_px - g_zgbTrkMax[tk]) / risk) : 0;
         double mfe = has ? (buy ? (g_zgbTrkMax[tk] - entry_px) / risk : (entry_px - g_zgbTrkMin[tk]) / risk) : 0;
         ZGB_PosxAdd(pids[j], ZGB_ToUtc(t_out), ph, has, mae, mfe);
         xi = ZGB_PosxFind(pids[j]);
         posx_changed = true;
        }
      if(tk >= 0) ZGB_TrkRemove(pids[j]);   // closed: free the tracker slot
      if(xi >= 0)
        {
         ps[n].phase = g_zgbXPh[xi];
         ps[n].has_ex = g_zgbXHas[xi];
         ps[n].mae = g_zgbXMae[xi];
         ps[n].mfe = g_zgbXMfe[xi];
        }
     }
   if(posx_changed) ZGB_PosxSave();
   return(ArraySize(ps));
  }

int ZGB_Outcome(const double pl) { return(pl > 0 ? 1 : (pl < 0 ? -1 : 0)); }

// ko[] keeps the outcome sequence of each group ("W", "L", "B"), oldest first, for streak / last-20 stats
void ZGB_GroupAdd(string &keys[], int &kn[], int &kw[], double &kr[], string &ko[], const string key, const int oc, const double r)
  {
   int i = 0, n = ArraySize(keys);
   for(; i < n; i++) if(keys[i] == key) break;
   if(i == n)
     {
      ArrayResize(keys, n + 1); ArrayResize(kn, n + 1); ArrayResize(kw, n + 1); ArrayResize(kr, n + 1); ArrayResize(ko, n + 1);
      keys[n] = key; kn[n] = 0; kw[n] = 0; kr[n] = 0; ko[n] = "";
     }
   kn[i]++;
   if(oc > 0) kw[i]++;
   kr[i] += r;
   ko[i] += (oc > 0 ? "W" : (oc < 0 ? "L" : "B"));
   if(StringLen(ko[i]) > 40) ko[i] = StringSubstr(ko[i], StringLen(ko[i]) - 20);
  }

string ZGB_GroupJson(string &keys[], int &kn[], int &kw[], double &kr[], string &ko[])
  {
   string out = "";
   for(int i = 0; i < ArraySize(keys); i++)
     {
      int len = StringLen(ko[i]), cur = 0, l20 = 0, l20w = 0;
      for(int j = len - 1; j >= 0 && StringGetCharacter(ko[i], j) == 'L'; j--) cur++;
      for(int j = (int)MathMax(0, len - 20); j < len; j++) { l20++; if(StringGetCharacter(ko[i], j) == 'W') l20w++; }
      out += (i > 0 ? "," : "") + StringFormat("\"%s\":{\"n\":%d,\"wins\":%d,\"win_pct\":%s,\"net_r\":%s,\"cur_ls\":%d,\"l20_n\":%d,\"l20_wins\":%d}",
                                                ZGB_Esc(keys[i]), kn[i], kw[i],
                                                ZGB_Num(kn[i] > 0 ? 100.0 * kw[i] / kn[i] : EMPTY_VALUE, 1), ZGB_Num(kr[i], 3),
                                                cur, l20, l20w);
     }
   return("{" + out + "}");
  }

string ZGB_Date(const datetime t) { return(StringSubstr(TimeToString(t, TIME_DATE), 0, 10)); }   // "YYYY.MM.DD"

string ZGB_IsoDate(const datetime t)
  {
   MqlDateTime d;
   TimeToStruct(t, d);
   return(StringFormat("%04d-%02d-%02d", d.year, d.mon, d.day));
  }

// Statistics of the positions closed in [from, now] (server time). No times, prices or tickets leave here.
string ZGB_AggJson(ZgbPos &ps[], const datetime from, const bool with_grid)
  {
   int total = ArraySize(ps);
   int idx[];
   int m = 0;
   for(int i = 0; i < total; i++)
      if(ps[i].t_out >= from) { ArrayResize(idx, m + 1); idx[m++] = i; }
   for(int i = 1; i < m; i++)   // order by exit time
     {
      int v = idx[i], j = i - 1;
      while(j >= 0 && ps[idx[j]].t_out > ps[v].t_out) { idx[j + 1] = idx[j]; j--; }
      idx[j + 1] = v;
     }
   int wins = 0, losses = 0, be = 0, max_ls = 0, cur_ls = 0, lead_ls = 0, n_r = 0, n_ex = 0;
   int no_sl = 0, risk_cap = 0, news = 0, daily_cap = 0;
   bool lead_open = true;
   double net_r = 0, gw = 0, gl = 0, net_pct = 0, cum = 0, peak = 0, dd = 0, mae = 0, mfe = 0;
   string mk[], pk[], dk[], xk[], mo[], po[], xo[];
   int mn[], mw[], pn[], pw[], xn[], xw[];
   double mr[], pr[], dv[], xr[];
   string sdk[], sdo[], ssk[], sso[];   // 2.7: by_side / by_session groups
   int sdn[], sdw[], ssn[], ssw[];
   double sdr[], ssr[];
   int gn[42], gw2[42];
   ArrayInitialize(gn, 0);
   ArrayInitialize(gw2, 0);
   for(int q = 0; q < m; q++)
     {
      int i = idx[q];
      int oc = ZGB_Outcome(ps[i].pl);
      if(oc > 0) wins++; else if(oc < 0) losses++; else be++;
      if(oc < 0) { cur_ls++; if(cur_ls > max_ls) max_ls = cur_ls; if(lead_open) lead_ls++; }
      else { cur_ls = 0; lead_open = false; }
      if(ps[i].has_r) { n_r++; net_r += ps[i].r; if(ps[i].r > 0) gw += ps[i].r; else gl -= ps[i].r; }
      net_pct += ps[i].pl;
      cum += ps[i].pl;
      if(cum > peak) peak = cum;
      if(peak - cum > dd) dd = peak - cum;
      if(ps[i].no_sl) no_sl++;
      if(ps[i].risk_over) risk_cap++;
      if(ps[i].news) news++;
      if(ps[i].has_ex) { n_ex++; mae += ps[i].mae; mfe += ps[i].mfe; }
      double rr = ps[i].has_r ? ps[i].r : 0;
      ZGB_GroupAdd(mk, mn, mw, mr, mo, ps[i].mcode, oc, rr);
      ZGB_GroupAdd(pk, pn, pw, pr, po, ps[i].phase, oc, rr);
      ZGB_GroupAdd(xk, xn, xw, xr, xo, ps[i].mcode + ":" + ps[i].phase, oc, rr);
      ZGB_GroupAdd(sdk, sdn, sdw, sdr, sdo, ZGB_SideKey(ps[i].side), oc, rr);   // 2.7
      ZGB_GroupAdd(ssk, ssn, ssw, ssr, sso, ps[i].sess, oc, rr);                // 2.7
      // daily cap: closed P/L per server day
      string day = ZGB_Date(ps[i].t_out);
      int di = 0, nd = ArraySize(dk);
      for(; di < nd; di++) if(dk[di] == day) break;
      if(di == nd) { ArrayResize(dk, nd + 1); ArrayResize(dv, nd + 1); dk[nd] = day; dv[nd] = 0; }
      dv[di] += ps[i].pl;
      // coarse grid: weekday x 4-hour bucket of the entry time (UTC)
      MqlDateTime t;
      TimeToStruct((datetime)ZGB_ToUtc(ps[i].t_in), t);
      int cell = ((t.day_of_week + 6) % 7) * 6 + t.hour / 4;
      gn[cell]++;
      if(oc > 0) gw2[cell]++;
     }
   for(int i = 0; i < ArraySize(dv); i++) if(dv[i] <= -InpGhDailyCapPct) daily_cap++;
   string grid = "";
   if(with_grid)
     {
      string wdn[7] = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"};
      for(int w = 0; w < 7; w++)
        {
         string row = "";
         for(int b = 0; b < 6; b++)
           {
            int c = w * 6 + b;
            if(gn[c] == 0) continue;
            string cellj = (gn[c] >= ZGB_MIN_CELL_N) ? StringFormat("{\"n\":%d,\"wins\":%d}", gn[c], gw2[c])
                                                     : StringFormat("{\"n\":%d}", gn[c]);   // small cell: count only
            row += (StringLen(row) > 0 ? "," : "") + StringFormat("\"%02d\":%s", b * 4, cellj);
           }
         if(StringLen(row) > 0) grid += (StringLen(grid) > 0 ? "," : "") + "\"" + wdn[w] + "\":{" + row + "}";
        }
      grid = ",\"grid\":{" + grid + "}";
     }
   int n = m;
   string ext = ",\"by_side\":" + ZGB_GroupJson(sdk, sdn, sdw, sdr, sdo) +
                ",\"by_session\":" + ZGB_GroupJson(ssk, ssn, ssw, ssr, sso);   // 2.7
   string out = StringFormat(
      "{\"from\":\"%s\",\"to\":\"%s\",\"n\":%d,\"wins\":%d,\"losses\":%d,\"be\":%d,\"win_pct\":%s,\"net_r\":%s,"
      "\"avg_r\":%s,\"gross_win_r\":%s,\"gross_loss_r\":%s,\"pf\":%s,\"net_pct\":%s,\"max_ls\":%d,\"cur_ls\":%d,"
      "\"lead_ls\":%d,\"dd_pct\":%s,\"n_r\":%d,\"n_ex\":%d,\"mae_r\":%s,\"mfe_r\":%s,"
      "\"rule\":{\"no_sl\":%d,\"risk_cap\":%d,\"news\":%d,\"daily_cap\":%d},\"by_method\":%s,\"by_phase\":%s,"
      "\"by_method_phase\":%s%s%s}",
      ZGB_IsoDate(from), ZGB_IsoDate(TimeTradeServer()), n, wins, losses, be,
      ZGB_Num(wins + losses > 0 ? 100.0 * wins / (wins + losses) : EMPTY_VALUE, 1), ZGB_Num(net_r, 3),
      ZGB_Num(n > 0 ? net_r / n : EMPTY_VALUE, 3), ZGB_Num(gw, 3), ZGB_Num(gl, 3),
      ZGB_Num(gl > 0 ? gw / gl : EMPTY_VALUE, 2), ZGB_Num(net_pct, 3), max_ls, cur_ls, lead_ls, ZGB_Num(dd, 3),
      n_r, n_ex, ZGB_Num(n_ex > 0 ? mae / n_ex : EMPTY_VALUE, 3), ZGB_Num(n_ex > 0 ? mfe / n_ex : EMPTY_VALUE, 3),
      no_sl, risk_cap, news, daily_cap, ZGB_GroupJson(mk, mn, mw, mr, mo), ZGB_GroupJson(pk, pn, pw, pr, po),
      ZGB_GroupJson(xk, xn, xw, xr, xo), ext, grid);
   return(out);
  }

// Builds one aggregate (v2) report and queues it. ts is rounded down to round_sec.
bool ZGB_BuildAggReport(const string reason, const long round_sec)
  {
   datetime now_srv = TimeTradeServer();
   MqlDateTime t;
   TimeToStruct(now_srv, t);
   t.hour = 0; t.min = 0; t.sec = 0;
   datetime day0 = StructToTime(t);
   t.day = 1;
   datetime month0 = StructToTime(t);
   datetime week0 = now_srv - 7 * 86400;
   datetime r30 = now_srv - 30 * 86400;
   ZgbPos ps[];
   ZGB_LoadPositions((datetime)MathMin((long)r30, (long)month0), ps);
   long ts = (long)TimeGMT();
   if(round_sec > 0) ts -= ts % round_sec;
   string rid = g_zgbAcct + "-" + IntegerToString(ts);
   string mkt = ZGB_MarketJson(false);
   string shd = (StringLen(g_zgbShadow) > 0) ? ",\"shadow\":" + g_zgbShadow : "";   // 2.5
   string pds = StringFormat("{\"day\":%s,\"week\":%s,\"mtd\":%s,\"r30\":%s}",
                             ZGB_AggJson(ps, day0, false), ZGB_AggJson(ps, week0, false), ZGB_AggJson(ps, month0, true),
                             ZGB_AggJson(ps, r30, false));
   string eqj = ZGB_EquityJson(false), flg = ZGB_FlagsJson();
   bool with_ctx = (StringLen(g_zgbCtx) > 0);
   string data = "", body = "";
   for(int pass = 0; pass < 3; pass++)   // a body over InpGhMaxPayloadBytes is rebuilt without mkt.ctx (2.4), then also without shadow (2.5)
     {
      data = StringFormat("{\"ea\":\"Zumi\",\"ver\":\"%s\",\"reason\":\"%s\",\"day\":\"%s\",\"phase\":\"%s\","
                          "\"eq\":%s,\"mkt\":%s,\"periods\":%s,\"flags\":%s%s}",
                          ZGB_VERSION, ZGB_Esc(reason), ZGB_IsoDate(now_srv), ZGB_Esc(g_zgbPhase),
                          eqj, mkt, pds, flg, shd);
      body = StringFormat("{\"event_type\":\"ea_report\",\"client_payload\":{\"v\":2,\"kind\":\"ea_agg\",\"acct\":\"%s\","
                          "\"rid\":\"%s\",\"part\":1,\"parts\":1,\"ts\":%I64d,\"data\":%s}}", g_zgbAcct, rid, ts, data);
      if(ZGB_Utf8Len(body) <= (int)MathMax(4000, InpGhMaxPayloadBytes)) break;
      if(with_ctx) { with_ctx = false; mkt = ZGB_MarketJson(false, false); }
      else if(StringLen(shd) > 0) shd = "";
      else break;
     }
   if(!ZGB_Enqueue(body, ts, 1)) { Print("ZumiGitHubBridge: could not write the report queue file"); return(false); }
   ArrayResize(g_zgbFlagJson, 0);
   ArrayResize(g_zgbLogMsg, 0);   // log lines are never published in aggregate mode
   ArrayResize(g_zgbLogT, 0);
   g_zgbDirty = false;
   g_zgbDirtySlot = -1;
   g_zgbFlagSlot = -1;
   return(true);
  }

string ZGB_LogJson(const int max_bytes)
  {
   // newest lines win when the budget is tight
   string out = "";
   int used = 2;
   for(int i = ArraySize(g_zgbLogMsg) - 1; i >= 0; i--)
     {
      string it = StringFormat("{\"t\":%I64d,\"m\":\"%s\"}", (long)g_zgbLogT[i], ZGB_Esc(g_zgbLogMsg[i]));
      int sz = ZGB_Utf8Len(it) + 1;
      if(used + sz > max_bytes) break;
      out = (StringLen(out) > 0) ? it + "," + out : it;
      used += sz;
     }
   return("[" + out + "]");
  }

string ZGB_FlagsJson(void)
  {
   string out = "";
   for(int i = 0; i < ArraySize(g_zgbFlagJson); i++) out += (i > 0 ? "," : "") + g_zgbFlagJson[i];
   return("[" + out + "]");
  }

string ZGB_Envelope(const string rid, const int part, const int parts, const long ts, const string data)
  {
   return(StringFormat("{\"event_type\":\"ea_report\",\"client_payload\":{\"v\":1,\"kind\":\"ea_report\",\"acct\":\"%s\","
                       "\"rid\":\"%s\",\"part\":%d,\"parts\":%d,\"ts\":%I64d,\"data\":%s}}",
                       g_zgbAcct, rid, part, parts, ts, data));
  }

bool ZGB_Enqueue(const string body, const long ts, const int part)
  {
   string name = StringFormat("zumi_gh_q_%I64d_%03d_%04d.json", ts, part, MathRand() % 10000);
   return(ZGB_WriteFile(name, body));
  }

// Build the report and put it in the local queue (network sending happens in ZGB_FlushQueue()).
bool ZGB_BuildReport(const string reason, const long round_sec = 0)
  {
   if(!InpGhEnable || !g_zgbNetOk) return(false);
   if(StringLen(g_zgbToken) == 0) return(false);
   bool ok = (InpGhReportMode == 0) ? ZGB_BuildAggReport(reason, round_sec) : ZGB_BuildDetailReport(reason);
   if(ok)   // 2.4: one report per slot is enough for the heartbeat
     {
      g_zgbRptSlot = (long)TimeGMT() / ((long)MathMax(1, InpGhReportEveryMin) * 60);
      GlobalVariableSet(ZGB_GvName("rslot"), (double)g_zgbRptSlot);
     }
   return(ok);
  }

// DETAIL MODE (schema v1, PRIVATE repositories only): every closing deal, split into parts if needed.
bool ZGB_BuildDetailReport(const string reason)
  {
   string items[];
   ulong max_k;
   datetime max_t;
   ZGB_CollectTrades(items, max_k, max_t);
   long ts = (long)TimeGMT();
   string rid = g_zgbAcct + "-" + IntegerToString(ts);
   string common = StringFormat("\"ea\":\"Zumi\",\"ver\":\"%s\",\"reason\":\"%s\",\"phase\":\"%s\",\"eq\":%s,\"mkt\":%s",
                                ZGB_VERSION, ZGB_Esc(reason), ZGB_Esc(g_zgbPhase), ZGB_EquityJson(), ZGB_MarketJson());
   int limit = (int)MathMax(4000, InpGhMaxPayloadBytes);
   int overhead = ZGB_Utf8Len(ZGB_Envelope(rid, 99, 99, ts, "{" + common + ",\"trades\":[],\"log\":[],\"flags\":[]}"));
   string flags = ZGB_FlagsJson();
   string logs = ZGB_LogJson((limit - overhead - ZGB_Utf8Len(flags)) / 2);
   int first_budget = limit - overhead - ZGB_Utf8Len(flags) - ZGB_Utf8Len(logs);
   int other_budget = limit - overhead;
   // greedy split of the trades into parts
   string groups[];
   int ng = 0;
   string cur = "";
   int cur_sz = 0;
   for(int i = 0; i < ArraySize(items); i++)
     {
      int sz = ZGB_Utf8Len(items[i]) + 1;
      int budget = (ng == 0) ? first_budget : other_budget;
      if(cur_sz > 0 && cur_sz + sz > budget)
        {
         ArrayResize(groups, ng + 1); groups[ng++] = cur;
         cur = ""; cur_sz = 0;
        }
      cur = (cur_sz > 0) ? cur + "," + items[i] : items[i];
      cur_sz += sz;
     }
   ArrayResize(groups, ng + 1);
   groups[ng++] = cur;   // the last (possibly empty) group always exists: part 1 carries eq/mkt/log
   bool ok = true;
   for(int p = 0; p < ng; p++)
     {
      string data = "{" + common + ",\"trades\":[" + groups[p] + "],\"log\":" + (p == 0 ? logs : "[]") +
                    ",\"flags\":" + (p == 0 ? flags : "[]") + "}";
      ok = ZGB_Enqueue(ZGB_Envelope(rid, p + 1, ng, ts, data), ts, p + 1) && ok;
     }
   if(ok)
     {
      GlobalVariableSet(ZGB_GvName("ld_k"), (double)max_k);
      GlobalVariableSet(ZGB_GvName("ld_t"), (double)max_t);
      ArrayResize(g_zgbLogMsg, 0);
      ArrayResize(g_zgbLogT, 0);
      ArrayResize(g_zgbFlagJson, 0);
      g_zgbDirty = false;
      g_zgbDirtySlot = -1;
      g_zgbFlagSlot = -1;
     }
   else Print("ZumiGitHubBridge: could not write the report queue file");
   return(ok);
  }

// Public: build a report now (rate limits still apply to the network side).
bool ZGB_SendReport(const string reason = "manual")
  {
   bool ok = ZGB_BuildReport(reason);
   if(ok) g_zgbLastReport = TimeGMT();
   return(ok);
  }

//+------------------------------------------------------------------+
//| queue flush (one POST per call)                                  |
//+------------------------------------------------------------------+
int ZGB_QueueList(string &names[])
  {
   ArrayResize(names, 0);
   string name;
   long h = FileFindFirst(ZGB_QUEUE_GLOB, name, ZGB_FileFlags());
   if(h == INVALID_HANDLE) return(0);
   do
     {
      int n = ArraySize(names);
      ArrayResize(names, n + 1);
      names[n] = name;
     }
   while(FileFindNext(h, name));
   FileFindClose(h);
   // names start with the UTC timestamp: sort ascending = oldest first (ArraySort is numeric-only)
   for(int i = 1; i < ArraySize(names); i++)
     {
      string key = names[i];
      int j = i - 1;
      while(j >= 0 && names[j] > key) { names[j + 1] = names[j]; j--; }
      names[j + 1] = key;
     }
   return(ArraySize(names));
  }

void ZGB_FlushQueue(void)
  {
   datetime now = TimeGMT();
   if(now < g_zgbPostAfter || StringLen(g_zgbToken) == 0) return;
   if(now - g_zgbLastPost < 10) return;   // at most one dispatch every 10 s
   string names[];
   int n = ZGB_QueueList(names);
   if(n == 0) return;
   for(int i = 0; i < n - ZGB_MAX_QUEUE; i++) FileDelete(names[i], ZGB_FileFlags());   // cap the queue
   string name = names[(int)MathMax(0, n - ZGB_MAX_QUEUE)];
   string body;
   if(!ZGB_ReadFile(name, body)) { FileDelete(name, ZGB_FileFlags()); return; }
   string url = InpGhApiBase + "/repos/" + InpGhOwner + "/" + InpGhRepo + "/dispatches";
   string headers = ZGB_GhHeaders(true, "application/vnd.github+json") + "Content-Type: application/json\r\n";
   string resp;
   int code = ZGB_Http("POST", url, headers, body, resp);
   if(code == -2) return;
   g_zgbLastPost = now;
   if(code == 204 || code == 200)
     {
      FileDelete(name, ZGB_FileFlags());
      g_zgbPostFails = 0;
      return;
     }
   if(code == 400 || code == 413 || code == 422)
     {
      PrintFormat("ZumiGitHubBridge: dispatch rejected HTTP %d, dropping %s", code, name);
      FileDelete(name, ZGB_FileFlags());
      return;
     }
   g_zgbPostFails++;
   int mins = (int)MathMin(60, MathPow(2, MathMin(g_zgbPostFails, 6)));
   if(code == 401 || code == 403 || code == 404)
     {
      mins = 30;
      if(!g_zgbWarned401)
        {
         PrintFormat("ZumiGitHubBridge: dispatch HTTP %d. Check the PAT: repository %s/%s only, permission Contents: Read and write, not expired.",
                     code, InpGhOwner, InpGhRepo);
         g_zgbWarned401 = true;
        }
     }
   g_zgbPostAfter = now + mins * 60;
   PrintFormat("ZumiGitHubBridge: dispatch failed (%d), %d queued, retry in %d min", code, n, mins);
  }

//+------------------------------------------------------------------+
//| optional instant channels                                        |
//+------------------------------------------------------------------+
void ZGB_FlushNotify(void)
  {
   // best effort, no retries: a chat message is dropped after one attempt
   string resp;
   if(ArraySize(g_zgbTgQ) > 0)
     {
      int code = ZGB_Http("POST", "https://api.telegram.org/bot" + InpTgBotToken + "/sendMessage",
                          "Content-Type: application/json\r\n",
                          "{\"chat_id\":\"" + ZGB_Esc(InpTgChatId) + "\",\"text\":\"" + ZGB_Esc(g_zgbTgQ[0]) + "\"}", resp);
      if(code != -2) ZGB_Pop(g_zgbTgQ);
      if(code > 0 && code != 200) PrintFormat("ZumiGitHubBridge: Telegram HTTP %d", code);
      return;
     }
   if(ArraySize(g_zgbDcQ) > 0)
     {
      int code = ZGB_Http("POST", InpDcWebhookUrl, "Content-Type: application/json\r\n",
                          "{\"content\":\"" + ZGB_Esc(g_zgbDcQ[0]) + "\"}", resp);
      if(code != -2) ZGB_Pop(g_zgbDcQ);
      if(code > 0 && code != 204 && code != 200) PrintFormat("ZumiGitHubBridge: Discord HTTP %d", code);
     }
  }

//+------------------------------------------------------------------+
//| scheduler: call from OnTimer (e.g. EventSetTimer(10))            |
//+------------------------------------------------------------------+
void ZGB_OnTimer(void)
  {
   if(!InpGhEnable || !g_zgbNetOk) return;
   g_zgbInTimer = true;
   g_zgbBudget = 1;   // at most ONE blocking WebRequest per timer call
   datetime now = TimeGMT();

   // 1) decide whether a report is due (building is local, no network)
   if(StringLen(g_zgbToken) > 0)
     {
      MqlDateTime st;
      TimeToStruct(TimeTradeServer(), st);
      int day_key = st.year * 10000 + st.mon * 100 + st.day;
      bool day_end = (st.hour > InpGhDayEndHour || (st.hour == InpGhDayEndHour && st.min >= InpGhDayEndMinute));
      // Reports leave only at slot boundaries: something that happened in slot k is reported at the
      // start of slot k+1 with ts rounded to that boundary, so report times do not reveal trade times.
      long slot_len = (long)MathMax(1, InpGhReportEveryMin) * 60;
      long slot = (long)now / slot_len;
      long fslot = (long)now / 900;
      if(g_zgbDirty && g_zgbDirtySlot < 0) g_zgbDirtySlot = slot;
      if(ArraySize(g_zgbFlagJson) > 0 && g_zgbFlagSlot < 0) g_zgbFlagSlot = fslot;
      if(day_end && day_key != g_zgbDayEndKey)
        {
         if(ZGB_BuildReport("dayend", 900))
           {
            g_zgbDayEndKey = day_key;
            GlobalVariableSet(ZGB_GvName("dayend"), (double)day_key);
            g_zgbLastReport = now;
           }
        }
      else if(g_zgbFlagSlot >= 0 && fslot > g_zgbFlagSlot)
        {
         if(ZGB_BuildReport("flag", 900)) { g_zgbLastFlagReport = now; g_zgbLastReport = now; }
        }
      else if(g_zgbDirtySlot >= 0 && slot > g_zgbDirtySlot)
        {
         if(ZGB_BuildReport("periodic", slot_len)) g_zgbLastReport = now;
        }
      else if(g_zgbDirtySlot < 0 && g_zgbFlagSlot < 0 && slot != g_zgbRptSlot && ZGB_CtxFresh(slot_len))
        {
         // 2.4 heartbeat: market context once per slot even without a closed trade. Never while a closing deal or a
         // flag is pending (those go out with their own report at the next boundary, so trade times stay hidden).
         if(ZGB_BuildReport("heartbeat", slot_len)) g_zgbLastReport = now;
        }
     }

   // 2) one network action, by priority: instant channel > report queue > latest.json
   if(g_zgbBudget > 0) ZGB_FlushNotify();
   if(g_zgbBudget > 0) ZGB_FlushQueue();
   if(g_zgbBudget > 0 && now >= g_zgbNextRead) ZGB_FetchLatest();
   if(InpGhControlEnable) ZGB_ReadControl(g_zgbBudget > 0);
   g_zgbInTimer = false;
  }

// Call from OnDeinit: no network here, pending trades/log lines go to the queue file.
void ZGB_Deinit(void)
  {
   if(!InpGhEnable || !g_zgbNetOk || StringLen(g_zgbToken) == 0) return;
   if(g_zgbDirty || ArraySize(g_zgbFlagJson) > 0 || (InpGhReportMode != 0 && ArraySize(g_zgbLogMsg) > 0))
      ZGB_BuildReport("deinit", (long)MathMax(1, InpGhReportEveryMin) * 60);
  }

#endif // ZUMI_GITHUB_BRIDGE_MQH

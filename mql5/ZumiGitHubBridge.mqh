//+------------------------------------------------------------------+
//|                                             ZumiGitHubBridge.mqh |
//|  Zumi XAU EA <-> GitHub repo "sakura" data bot                   |
//|                                                                  |
//|  READ : data/latest.json (macro / calendar context) -> ZGB_ReadLatest()
//|  WRITE: trade + analysis reports -> repository_dispatch "ea_report"
//|         (ZGB_SendReport / automatic from ZGB_OnTimer)            |
//|  OPTIONAL instant channels: Telegram bot, Discord webhook (off)  |
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
//|    money only as % of balance; the token is never printed.       |
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

#define ZGB_VERSION "1.0"

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
input int    InpGhReportEveryMin  = 30;                  // Min minutes between periodic reports
input int    InpGhDayEndHour      = 23;                  // Day-end report, server hour
input int    InpGhDayEndMinute    = 45;                  // Day-end report, server minute
input string InpGhAccountSalt     = "change-me";         // Salt for the account hash (keep private)
input long   InpGhMagicFilter     = -1;                  // Report only this magic (-1 = all)
input string InpGhSymbolFilter    = "";                  // Report only this symbol ("" = all)
input string InpGhMarketSymbol    = "";                  // Symbol for market context ("" = chart)
input int    InpGhMaxPayloadBytes = 30000;               // Max bytes per dispatch request
input bool   InpGhUseCommonFiles  = false;               // Cache/queue in Common\Files
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

#define ZGB_MAX_LOG    120
#define ZGB_MAX_FLAGS  20
#define ZGB_MAX_QUEUE  500
#define ZGB_MAX_TRACK  200
#define ZGB_CACHE_FILE "zumi_gh_latest.json"
#define ZGB_QUEUE_GLOB "zumi_gh_q_*.json"

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
   if(GlobalVariableCheck(ZGB_GvName("dayend"))) g_zgbDayEndKey = (int)GlobalVariableGet(ZGB_GvName("dayend"));
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
   PrintFormat("ZumiGitHubBridge %s: repo %s/%s@%s, %s read, token %s, account hash %s",
               ZGB_VERSION, InpGhOwner, InpGhRepo, InpGhBranch, InpGhPrivateRepo ? "API" : "raw",
               StringLen(g_zgbToken) > 0 ? "set" : "NOT set (reports disabled)", g_zgbAcct);
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
//| EA-side logging / flags / phase                                  |
//+------------------------------------------------------------------+
void ZGB_SetPhase(const string phase) { g_zgbPhase = StringSubstr(phase, 0, 48); }

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

bool ZGB_PassFilter(const string sym, const long magic)
  {
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
string ZGB_EquityJson(void)
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
   return(StringFormat("{\"day_pl_pct\":%s,\"eq_day_pct\":%s,\"float_pct\":%s,\"margin_level\":%s}",
                       ZGB_Num(day_pl, 3), ZGB_Num(eq_day, 3), ZGB_Num(flt, 3),
                       ZGB_Num(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 1)));
  }

string ZGB_MarketJson(void)
  {
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
   return(StringFormat("{\"sym\":\"%s\",\"bid\":%s,\"pc\":%s,\"d1_open\":%s,\"atr\":%s,\"move\":%s,\"move_atr\":%s,\"spread\":%s}",
                       ZGB_Esc(sym), ZGB_Num(bid, dg), ZGB_Num(pc, dg), ZGB_Num(op, dg), ZGB_Num(atr, dg),
                       ZGB_Num(mv, dg), ZGB_Num(mva, 3), ZGB_Num((ask > 0 && bid > 0) ? ask - bid : EMPTY_VALUE, dg)));
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

// Build the report (split into parts if needed) and put it in the local queue.
// Network sending happens in ZGB_FlushQueue() from ZGB_OnTimer().
bool ZGB_BuildReport(const string reason)
  {
   if(!InpGhEnable || !g_zgbNetOk) return(false);
   if(StringLen(g_zgbToken) == 0) return(false);
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
      if(day_end && day_key != g_zgbDayEndKey)
        {
         if(ZGB_BuildReport("dayend"))
           {
            g_zgbDayEndKey = day_key;
            GlobalVariableSet(ZGB_GvName("dayend"), (double)day_key);
            g_zgbLastReport = now;
           }
        }
      else if(ArraySize(g_zgbFlagJson) > 0 && now - g_zgbLastFlagReport >= 300)
        {
         if(ZGB_BuildReport("flag")) { g_zgbLastFlagReport = now; g_zgbLastReport = now; }
        }
      else if(g_zgbDirty && (long)(now - g_zgbLastReport) >= (long)MathMax(1, InpGhReportEveryMin) * 60)
        {
         if(ZGB_BuildReport("periodic")) g_zgbLastReport = now;
        }
     }

   // 2) one network action, by priority: instant channel > report queue > latest.json
   if(g_zgbBudget > 0) ZGB_FlushNotify();
   if(g_zgbBudget > 0) ZGB_FlushQueue();
   if(g_zgbBudget > 0 && now >= g_zgbNextRead) ZGB_FetchLatest();
   g_zgbInTimer = false;
  }

// Call from OnDeinit: no network here, pending trades/log lines go to the queue file.
void ZGB_Deinit(void)
  {
   if(!InpGhEnable || !g_zgbNetOk || StringLen(g_zgbToken) == 0) return;
   if(g_zgbDirty || ArraySize(g_zgbLogMsg) > 0 || ArraySize(g_zgbFlagJson) > 0)
      ZGB_BuildReport("deinit");
  }

#endif // ZUMI_GITHUB_BRIDGE_MQH

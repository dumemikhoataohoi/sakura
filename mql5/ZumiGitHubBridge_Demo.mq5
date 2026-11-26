//+------------------------------------------------------------------+
//|                                       ZumiGitHubBridge_Demo.mq5  |
//|  Minimal example of wiring ZumiGitHubBridge.mqh into an EA.      |
//|  It does NOT trade: it shows the macro context on the chart,     |
//|  logs phase changes and raises a flag on a local daily-loss cap. |
//|                                                                  |
//|  Install: copy both files to MQL5\Experts\Zumi\ , compile this   |
//|  file in MetaEditor, allow the WebRequest URLs (see the .mqh     |
//|  header), then set InpGhEnable=true and the token/salt inputs.   |
//+------------------------------------------------------------------+
#property copyright "Zumi XAU EA project"
#property version   "1.00"
#property description "Demo of the Zumi GitHub bridge (read latest.json, send reports)."

#include "ZumiGitHubBridge.mqh"

input group "=== Demo ==="
input int    InpTimerSec        = 10;    // Timer period (s): the bridge does <= 1 request per tick
input int    InpNewsBlockMin    = 30;    // Block new entries N minutes before a high-impact event
input double InpLocalDayLossPct = 2.5;   // Raise a flag when equity vs day start drops below -X %

ZgbLatest g_ctx;              // last macro context (refreshed in OnTimer, used in OnTick)
string    g_lastPhase = "";
bool      g_lossFlagged = false;
int       g_lossFlagDay = 0;

int OnInit()
  {
   ZGB_Init();
   EventSetTimer(MathMax(1, InpTimerSec));
   ZGB_ResetLatest(g_ctx);
   ZGB_SetPhase("INIT");
   ZGB_Log("demo EA started on " + _Symbol);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   ZGB_Deinit();          // queues unsent trades/log lines; no network in OnDeinit
   Comment("");
  }

void OnTick()
  {
   ZGB_OnTick();          // MAE/MFE tracking only, no network

   // Example: your strategy decides the phase; log each change once.
   string phase = (g_ctx.ok && g_ctx.vix_high == 1) ? "RISK_OFF" : "NORMAL";
   if(phase != g_lastPhase)
     {
      ZGB_SetPhase(phase);
      ZGB_Log("PHASE " + g_lastPhase + " -> " + phase);
      g_lastPhase = phase;
     }

   if(NewsBlockActive())
      return;             // e.g. skip new entries; manage open trades as usual
   // ... your trading logic here ...
  }

void OnTimer()
  {
   ZGB_OnTimer();                      // reports, queue flush, latest.json refresh (<= 1 request)
   ZGB_ReadLatest(g_ctx, false);       // parse the cache only: never blocks
   CheckLocalDayLoss();
   ShowContext();
  }

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
  {
   ZGB_OnTradeTransaction(trans);      // closing deals -> report due (batched, rate-limited)
  }

//--- true from InpNewsBlockMin minutes before the next NFP/CPI/FOMC until it is released
bool NewsBlockActive()
  {
   if(!g_ctx.ok || g_ctx.next_event_utc == 0) return(false);   // no data: trade normally
   long secs = (long)(g_ctx.next_event_utc - TimeGMT());
   return(secs >= 0 && secs <= (long)InpNewsBlockMin * 60);
  }

void CheckLocalDayLoss()
  {
   MqlDateTime t;
   TimeToStruct(TimeTradeServer(), t);
   int day = t.year * 10000 + t.mon * 100 + t.day;
   if(day != g_lossFlagDay) { g_lossFlagDay = day; g_lossFlagged = false; }
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_lossFlagged || bal <= 0) return;
   double pct = (eq - bal) / bal * 100.0;   // simple floating check for the demo
   if(pct <= -InpLocalDayLossPct)
     {
      ZGB_Flag("day_loss", StringFormat("floating P/L %.2f%% <= -%.2f%%", pct, InpLocalDayLossPct));
      g_lossFlagged = true;
     }
  }

void ShowContext()
  {
   if(!g_ctx.ok)
     {
      Comment("Zumi bridge: no macro data (" + g_ctx.reason + ") - trading normally");
      return;
     }
   string ev = (g_ctx.next_event_utc > 0)
               ? StringFormat("%s at %s UTC%s", g_ctx.next_event, TimeToString(g_ctx.next_event_utc, TIME_DATE | TIME_MINUTES),
                              g_ctx.next_event_unc == 1 ? " (unconfirmed)" : "")
               : "none listed";
   Comment(StringFormat("Zumi bridge: data age %.1f h\nVIX %s (%s, %s%%)  10y %s  real 10y %s  USD %s\nNext event: %s%s",
                        g_ctx.age_h, ZGB_Num(g_ctx.vix, 2), g_ctx.vix_date, ZGB_Num(g_ctx.vix_chg_pct, 1),
                        ZGB_Num(g_ctx.dgs10, 2), ZGB_Num(g_ctx.dfii10, 2), ZGB_Num(g_ctx.usd, 2), ev,
                        NewsBlockActive() ? "  [NEWS BLOCK]" : ""));
  }
//+------------------------------------------------------------------+

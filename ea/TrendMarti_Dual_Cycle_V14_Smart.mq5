//+------------------------------------------------------------------+
//|                         TrendMarti_Dual_Cycle_V14_Smart.mq5      |
//|  Dual buy/sell grid baskets with a known, capped worst case.     |
//|  Built from V13_Recovery. Tested on EURUSD M5 (see ea/README.md) |
//+------------------------------------------------------------------+
#property copyright "Faris / trading-dashboard"
#property version   "14.00"
#property description "Dual grid: trend side may grow, counter side stays small, hard stop per basket, lot from equity."

#include <Trade\Trade.mqh>

//--- Inputs
input group "--- Risiko (paling penting) ---"
input double InpRiskPct          = 4.0;    // Rugi maks 1 basket penuh (% equity), lot awal dihitung dari ini
input double InpBasketMaxLossPct = 6.0;    // Pengaman uang: tutup basket kalau rugi > % equity ini (gap/slippage)
input double InpMaxDDPercent     = 20.0;   // DD dari equity puncak -> tutup semua + jeda
input int    InpPauseHours       = 24;     // Lama jeda setelah DD guard
input double InpFixedLot         = 0.0;    // >0 = pakai lot tetap ini (abaikan InpRiskPct)

input group "--- Grid ---"
input double InpLotMultiplier    = 1.3;    // Multiplier lot per layer
input int    InpMaxLayers        = 6;      // Layer maks basket searah tren
input int    InpCounterLayers    = 3;      // Layer maks basket melawan tren
input double InpGridATR          = 1.5;    // Jarak grid = x ATR(H1,14)
input double InpMinGridPips      = 8.0;    // Jarak grid minimum (pips)
input double InpGridExpansion    = 1.2;    // Jarak layer berikutnya x ini
input double InpStopSteps        = 1.0;    // Hard stop: sekian langkah grid setelah layer terakhir

input group "--- Entry ---"
input ENUM_TIMEFRAMES InpRSITF   = PERIOD_M5;
input int    InpRSIPeriod        = 6;
input double InpRSIWithTrend     = 35;     // Buy searah tren jika RSI < ini (sell > 100-ini)
input double InpRSICounter       = 20;     // Buy melawan tren hanya jika RSI < ini (sell > 100-ini)
input ENUM_TIMEFRAMES InpTrendTF = PERIOD_H1;
input int    InpTrendEMA         = 200;    // Filter tren: close H1 vs EMA200
input int    InpATRPeriod        = 14;

input group "--- Take profit ---"
input double InpTPATR            = 0.8;    // TP basket = rata-rata + x ATR (bersih swap & komisi)
input int    InpRecoveryStart    = 3;      // Mulai layer ini pakai TP kecil
input double InpRecoveryTPATR    = 0.25;   // TP recovery = x ATR
input double InpCommissionPerLot = 0.0;    // Komisi round-turn per lot (akun ECN misal 7)
input double InpMaxBasketHours   = 0;      // >0 = tutup basket yang lebih tua dari ini

input group "--- Filter waktu & berita (jam UTC/GMT) ---"
input int    InpNoNewFromHour    = 20;     // Tidak buka basket baru dari jam ini...
input int    InpNoNewToHour      = 1;      // ...sampai jam ini (rollover spread lebar)
input int    InpFridayStopHour   = 18;     // Jumat: tidak buka basket baru setelah jam ini
input bool   InpFridayClose      = false;  // Jumat 20:45 UTC tutup semua (hindari gap weekend)
input bool   InpUseNewsFilter    = true;   // Kalender MT5: jeda saat berita high impact EUR/USD
input int    InpNewsMinutes      = 30;     // Jeda +- menit di sekitar berita
input bool   InpUseTimeFilter    = true;   // Libur akhir tahun
input int    InpStopMonth        = 12;
input int    InpStopDay          = 15;

input group "--- Lain-lain ---"
input int    InpMaxSpreadPoints  = 25;     // Spread maks (points) untuk buka posisi
input int    InpMagicNum         = 141414;
input double InpVirtualCapital   = 10000.0;

//--- Globals
CTrade trade;
int    hRSI = INVALID_HANDLE, hEMA = INVALID_HANDLE, hATR = INVALID_HANDLE;
datetime g_pauseUntil = 0;
double   g_peakEquity = 0;

struct BasketInfo
  {
   int    count;
   double lots;
   double firstPrice;
   double lastLot;
   double profit;      // profit + swap - commission (account currency)
   datetime opened;
  };

//+------------------------------------------------------------------+
string GV(const string name, const int side)
  { return StringFormat("TM14_%d_%s_%s_%d", InpMagicNum, _Symbol, name, side); }

double GVGet(const string name, const int side, const double def = 0)
  { string k = GV(name, side); return GlobalVariableCheck(k) ? GlobalVariableGet(k) : def; }

void GVSet(const string name, const int side, const double v) { GlobalVariableSet(GV(name, side), v); }

//+------------------------------------------------------------------+
int OnInit()
  {
   hRSI = iRSI(_Symbol, InpRSITF, InpRSIPeriod, PRICE_CLOSE);
   hEMA = iMA(_Symbol, InpTrendTF, InpTrendEMA, 0, MODE_EMA, PRICE_CLOSE);
   hATR = iATR(_Symbol, InpTrendTF, InpATRPeriod);
   if(hRSI == INVALID_HANDLE || hEMA == INVALID_HANDLE || hATR == INVALID_HANDLE)
      return(INIT_FAILED);

   trade.SetExpertMagicNumber(InpMagicNum);
   trade.SetDeviationInPoints(30);
   long fm = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);   // bit flags, not a single value
   if((fm & SYMBOL_FILLING_FOK) != 0)      trade.SetTypeFilling(ORDER_FILLING_FOK);
   else if((fm & SYMBOL_FILLING_IOC) != 0) trade.SetTypeFilling(ORDER_FILLING_IOC);
   else                                    trade.SetTypeFilling(ORDER_FILLING_RETURN);

   g_peakEquity = MathMax(GVGet("peak", 0, 0), AccountInfoDouble(ACCOUNT_EQUITY));
   g_pauseUntil = (datetime)GVGet("pause", 0, 0);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   IndicatorRelease(hRSI); IndicatorRelease(hEMA); IndicatorRelease(hATR);
   Comment("");
  }

//+------------------------------------------------------------------+
double Buf(const int handle, const int shift)
  {
   double b[];
   if(CopyBuffer(handle, 0, shift, 1, b) != 1) return(EMPTY_VALUE);
   return(b[0]);
  }

// account money per 1.0 price move per 1.0 lot
double MoneyPerPrice()
  {
   double tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   return(ts > 0 ? tv / ts : 0);
  }

double NormLot(double lot, const bool down = false)
  {
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double mn   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double mx   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   lot = down ? MathFloor(lot / step) * step : MathRound(lot / step) * step;
   if(lot < mn) lot = down ? 0 : mn;
   return(NormalizeDouble(MathMin(lot, mx), 2));
  }

// Offset (in grid steps) of layer k, and of the hard stop after n layers
double LayerOffset(const int k)
  {
   double o = 0, d = 1;
   for(int j = 0; j < k; j++) { o += d; d *= InpGridExpansion; }
   return(o);
  }
double StopOffset(const int n)
  { return(LayerOffset(n - 1) + MathPow(InpGridExpansion, n - 1) * InpStopSteps); }

// Starting lot so that a full trend basket that hits its hard stop loses InpRiskPct of equity
double StartLot(const double equity, const double step)
  {
   if(InpFixedLot > 0) return(NormLot(InpFixedLot));
   double spread = SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double stop = StopOffset(InpMaxLayers), units = 0;
   for(int k = 0; k < InpMaxLayers; k++)
      units += MathPow(InpLotMultiplier, k) * ((stop - LayerOffset(k)) * step + spread);
   double mpp = MoneyPerPrice();
   if(units <= 0 || mpp <= 0) return(0);
   return(NormLot(equity * InpRiskPct / 100.0 / (units * mpp), true));
  }

//+------------------------------------------------------------------+
BasketInfo Scan(const ENUM_POSITION_TYPE type)
  {
   BasketInfo b; b.count = 0; b.lots = 0; b.firstPrice = 0; b.lastLot = 0; b.profit = 0; b.opened = 0;
   datetime first = 0, last = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(!PositionSelectByTicket(t)) continue;
      if(PositionGetInteger(POSITION_MAGIC) != InpMagicNum || PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != type) continue;
      double v = PositionGetDouble(POSITION_VOLUME);
      datetime ot = (datetime)PositionGetInteger(POSITION_TIME);
      b.count++;
      b.lots += v;
      b.profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP) - InpCommissionPerLot * v;
      if(first == 0 || ot < first) { first = ot; b.firstPrice = PositionGetDouble(POSITION_PRICE_OPEN); }
      if(ot >= last) { last = ot; b.lastLot = v; }
     }
   b.opened = first;
   return(b);
  }

void CloseBasket(const ENUM_POSITION_TYPE type, const string why)
  {
   for(int attempt = 0; attempt < 3; attempt++)
     {
      int left = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong t = PositionGetTicket(i);
         if(!PositionSelectByTicket(t)) continue;
         if(PositionGetInteger(POSITION_MAGIC) != InpMagicNum || PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
         if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != type) continue;
         if(!trade.PositionClose(t)) left++;
        }
      if(left == 0) break;
      Sleep(300);
     }
   int side = (type == POSITION_TYPE_BUY) ? 1 : -1;
   GlobalVariableDel(GV("step", side));
   PrintFormat("V14 close %s basket: %s", side > 0 ? "BUY" : "SELL", why);
  }

bool NewsBlackout()
  {
   if(!InpUseNewsFilter || MQLInfoInteger(MQL_TESTER)) return(false);   // calendar is not available in the tester
   datetime now = TimeTradeServer();
   MqlCalendarValue vals[];
   string cur[2] = {"USD", "EUR"};
   for(int c = 0; c < 2; c++)
     {
      if(CalendarValueHistory(vals, now - InpNewsMinutes * 60, now + InpNewsMinutes * 60, NULL, cur[c]) <= 0) continue;
      for(int i = 0; i < ArraySize(vals); i++)
        {
         MqlCalendarEvent ev;
         if(CalendarEventById(vals[i].event_id, ev) && ev.importance == CALENDAR_IMPORTANCE_HIGH)
            return(true);
        }
     }
   return(false);
  }

bool Open(const int side, const double lot, const string tag)
  {
   if(lot <= 0) return(false);
   double price = side > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double margin = 0;
   ENUM_ORDER_TYPE ot = side > 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcMargin(ot, _Symbol, lot, price, margin) || margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE) * 0.9)
     {
      Print("V14: margin tidak cukup untuk ", DoubleToString(lot, 2), " lot");
      return(false);
     }
   return(side > 0 ? trade.Buy(lot, _Symbol, 0, 0, 0, tag) : trade.Sell(lot, _Symbol, 0, 0, 0, tag));
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   datetime now = TimeCurrent();

   // 1) Drawdown from peak equity (not from balance)
   if(equity > g_peakEquity) { g_peakEquity = equity; GVSet("peak", 0, g_peakEquity); }
   if(g_peakEquity > 0 && equity <= g_peakEquity * (1 - InpMaxDDPercent / 100.0))
     {
      CloseBasket(POSITION_TYPE_BUY, "DD guard");
      CloseBasket(POSITION_TYPE_SELL, "DD guard");
      g_peakEquity = AccountInfoDouble(ACCOUNT_BALANCE);
      GVSet("peak", 0, g_peakEquity);
      g_pauseUntil = now + InpPauseHours * 3600;
      GVSet("pause", 0, (double)g_pauseUntil);
      return;
     }

   MqlDateTime g; TimeToStruct(TimeGMT(), g);
   if(InpFridayClose && g.day_of_week == 5 && (g.hour > 20 || (g.hour == 20 && g.min >= 45)))
     {
      CloseBasket(POSITION_TYPE_BUY, "Friday close");
      CloseBasket(POSITION_TYPE_SELL, "Friday close");
      return;
     }

   double rsi = Buf(hRSI, 1), ema = Buf(hEMA, 1), atr = Buf(hATR, 1);
   double h1c = iClose(_Symbol, InpTrendTF, 1);
   if(rsi == EMPTY_VALUE || ema == EMPTY_VALUE || atr == EMPTY_VALUE || h1c <= 0) return;
   int trend = h1c > ema ? 1 : -1;

   bool night = InpNoNewFromHour > InpNoNewToHour
                ? (g.hour >= InpNoNewFromHour || g.hour < InpNoNewToHour)
                : (g.hour >= InpNoNewFromHour && g.hour < InpNoNewToHour);
   MqlDateTime s; TimeToStruct(now, s);
   bool holiday = InpUseTimeFilter && s.mon == InpStopMonth && s.day >= InpStopDay;
   bool news = NewsBlackout();
   bool noNew = now < g_pauseUntil || night || (g.day_of_week == 5 && g.hour >= InpFridayStopHour) || holiday || news;
   bool spreadOk = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) <= InpMaxSpreadPoints;
   double mpp = MoneyPerPrice();

   string info = "";
   for(int side = 1; side >= -1; side -= 2)
     {
      ENUM_POSITION_TYPE type = side > 0 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
      BasketInfo b = Scan(type);
      bool withTrend = (side == trend);

      // --- no basket: maybe start one
      if(b.count == 0)
        {
         if(noNew || !spreadOk) continue;
         double lvl = withTrend ? InpRSIWithTrend : InpRSICounter;
         if(!((side > 0 && rsi < lvl) || (side < 0 && rsi > 100 - lvl))) continue;
         double step = MathMax(InpGridATR * atr, InpMinGridPips * 10 * _Point);
         double lot = StartLot(equity, step);
         if(lot <= 0) { info += "Equity terlalu kecil untuk lot minimum dengan risiko ini\n"; continue; }
         if(Open(side, lot, withTrend ? "V14 trend L1" : "V14 counter L1"))
           {
            int n = withTrend ? InpMaxLayers : InpCounterLayers;
            GVSet("step", side, step);
            GVSet("lot0", side, lot);
            GVSet("max", side, n);
            GVSet("trend", side, withTrend ? 1 : 0);
            GVSet("eq0", side, equity);
           }
         continue;
        }

      // --- existing basket
      double step = GVGet("step", side, 0);
      if(step <= 0)   // restarted without saved state: rebuild from now
        {
         step = MathMax(InpGridATR * atr, InpMinGridPips * 10 * _Point);
         GVSet("step", side, step); GVSet("lot0", side, b.lastLot / MathPow(InpLotMultiplier, b.count - 1));
         GVSet("max", side, MathMax(b.count, withTrend ? InpMaxLayers : InpCounterLayers));
         GVSet("trend", side, withTrend ? 1 : 0); GVSet("eq0", side, equity);
        }
      double lot0 = GVGet("lot0", side), eq0 = GVGet("eq0", side, equity);
      int maxL = (int)GVGet("max", side, InpMaxLayers);
      bool basketTrend = GVGet("trend", side, 1) > 0;

      double px = side > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double stopPx = b.firstPrice - side * StopOffset(maxL) * step;

      // hard stop: price past the last layer, or money loss past the cap
      if((side > 0 && px <= stopPx) || (side < 0 && px >= stopPx) || b.profit <= -eq0 * InpBasketMaxLossPct / 100.0)
        { CloseBasket(type, "hard stop"); continue; }
      if(InpMaxBasketHours > 0 && now - b.opened >= InpMaxBasketHours * 3600)
        { CloseBasket(type, "max hours"); continue; }

      // net take profit (profit + swap - commission)
      double tpDist = (b.count >= InpRecoveryStart ? InpRecoveryTPATR : InpTPATR) * atr;
      if(b.profit >= b.lots * tpDist * mpp)
        { CloseBasket(type, "TP"); continue; }

      // add a layer: only while the trend still agrees for a trend basket
      if(b.count < maxL && (withTrend || !basketTrend) && !noNew && spreadOk)
        {
         double level = b.firstPrice - side * LayerOffset(b.count) * step;
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK), bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         if((side > 0 && ask <= level) || (side < 0 && bid >= level))
            Open(side, NormLot(lot0 * MathPow(InpLotMultiplier, b.count)), "V14 L" + IntegerToString(b.count + 1));
        }
      info += StringFormat("%s: %d/%d layer, %.2f lot, P/L %.2f, stop %s\n", side > 0 ? "BUY" : "SELL",
                           b.count, maxL, b.lots, b.profit, DoubleToString(stopPx, _Digits));
     }
   if(!MQLInfoInteger(MQL_TESTER))
      Comment(StringFormat("V14 Smart | tren H1 %s | DD dari puncak %.1f%% %s\n%s",
              trend > 0 ? "NAIK" : "TURUN", (1 - equity / g_peakEquity) * 100,
              now < g_pauseUntil ? "| JEDA" : (news ? "| BERITA" : ""), info));
  }

//+------------------------------------------------------------------+
double OnTester()
  {
   double netProfit = TesterStatistics(STAT_PROFIT);
   double maxDDMoney = TesterStatistics(STAT_EQUITY_DD);
   double sharpe = TesterStatistics(STAT_SHARPE_RATIO);
   if(maxDDMoney > InpVirtualCapital) return(-maxDDMoney);
   return((netProfit * sharpe) / (maxDDMoney + 1.0));
  }
//+------------------------------------------------------------------+

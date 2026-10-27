"""
==============================================================================
ETF Ki Dukan - Portfolio Tracker  (GitHub Actions edition)
==============================================================================
Runs ONCE PER INVOCATION from a GitHub Actions schedule (see
.github/workflows/scan.yml): every 15 minutes, 9:15 AM - 3:30 PM IST, on NSE
trading days.

  1. Reads positions from portfolio.json (in the repo).
  2. Fetches prices, runs the exit engine, sends new Telegram alerts.
  3. Writes portfolio.json AND docs/state.json (dashboard reads the latter).

STRATEGY = identical to backtest_nse_split.py (split-capital / slot version):
    Intraday-touch rules (checked EVERY run, using today's High/Low so far):
      a. trailing active  -> stop = max(peak*(1-TRAIL_PCT%), TP price); low <= stop => trail_stop
      b. TP touched       -> still Rank #1 ? start trailing (no sell) : take_profit
      c. breakeven armed (+3% seen on an earlier day) and low <= entry+1% => breakeven_stop
      d. low <= entry -15%                                          => stop_loss
    Close-based rules (checked only from CLOSE_RULES_FROM, default 3:15 PM IST,
    because the backtest uses the daily CLOSE for these):
      e. price back at/above DMA (DMA_WINDOW)                        => mean_reversion
      f. EMA_FAST crosses below EMA_SLOW                             => ema_death_cross
      g. trailing + no longer Rank #1                               => trail_rank_lost
      h. held >= LATE_HOLD_START_DAY days and fast EMA(2,13) crosses below slow => late_hold_ema_exit
         (runs right after the normal EMA death-cross check, before max-holding)
      i. held >= MAX_HOLDING_DAYS: still Rank #1 ? extend : max_holding_period
    Entry signals (from ENTRY_SIGNAL_FROM, default 3:00 PM IST - backtest buys at the CLOSE):
      Capital is split into N_SLOTS slots. A slot is usable for a new buy when it holds no
      position, did not sell today (T+1 settlement) and fewer than MAX_CONCURRENT_POSITIONS
      positions remain open after today's exits. The pick is the best-ranked ETF (most
      below its DMA) that is not already held (ALLOW_SAME_ETF = False). -> Telegram "BUY" alert.
==============================================================================
"""

import os
import json
import shutil
from datetime import datetime, date, timedelta, timezone
from datetime import time as dtime

import pandas as pd

# ==============================================================================
# STRATEGY CONFIG - keep identical to your backtest script
# ==============================================================================

DMA_WINDOW = 10
INITIAL_CAPITAL = 20000
TRANSACTION_COST_PCT = 0.019    # % per trade side (used only for suggested buy quantity)

# ---- SPLIT-CAPITAL / SLOTS (same as backtest_nse_split.py) ----
N_SLOTS = 2                     # capital split into this many parts (2 = 50/50)
MAX_CONCURRENT_POSITIONS = 1    # 1 = one position at a time, 2 = both slots can be invested together
SETTLE_T1 = True                # sell proceeds usable from the NEXT trading day
ALLOW_SAME_ETF = False          # False = an ETF already held in another slot is skipped

TAKE_PROFIT_PCT = 3.0
STOP_LOSS_PCT = -8
EXIT_ON_MEAN_REVERSION = True
MAX_HOLDING_DAYS = 5

# ---- Trailing profit (when TP is touched and the ETF is still Rank #1) ----
TRAIL_ENABLED = True
TRAIL_PCT = 0.5       # exit when price falls this % below the peak
TRAIL_LOCK_TP = True  # stop never goes below the take-profit price

# ---- Breakeven stop ----
# Once +BREAKEVEN_TRIGGER_PCT% has been touched on an EARLIER day, the stop moves to
# entry +BREAKEVEN_STOP_PCT% (active from the next session, same as the backtest).
BREAKEVEN_ENABLED = True
BREAKEVEN_TRIGGER_PCT = 1.5
BREAKEVEN_STOP_PCT = 1

EXIT_ON_EMA_DEATH_CROSS = True
EMA_FAST = 5
EMA_SLOW = 40

# ---- Late-holding fast-EMA safety check ----
# Once a position is LATE_HOLD_START_DAY (calendar days) old and nothing else has
# exited it, a fast EMA(LATE_HOLD_EMA_FAST, LATE_HOLD_EMA_SLOW) death cross exits it
# early instead of waiting for MAX_HOLDING_DAYS. Close-based, like the backtest.
LATE_HOLD_EMA_EXIT = True
LATE_HOLD_START_DAY = 5
LATE_HOLD_EMA_FAST = 3
LATE_HOLD_EMA_SLOW = 13

# ---- Schedule / market clock (all IST) ----
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
CLOSE_RULES_FROM = dtime(14, 30)       # close-based rules are evaluated from this time
ENTRY_SIGNAL_FROM = dtime(15, 0)       # BUY signals are computed from this time ...
ENTRY_SIGNAL_UNTIL = dtime(15, 29)     # ... until just before the close (still time to place the order)
SCHEDULE_GATE = (dtime(9, 0), dtime(16, 0))   # scheduled runs outside this window just exit

USE_LIVE_PRICE = True
USE_MASTER_CALENDAR = False
PRICE_HISTORY_PERIOD = "6mo"

# ---- data sanity (mirrors backtest's bad-print / stale-price handling) ----
MAX_DAILY_MOVE_PCT = 20.0       # ETF whose last bar moved more than this is excluded from ranking
MAX_STALE_DAYS = 5              # ETF with no bar for this many calendar days is excluded from ranking

# ETF universe used ONLY for the "still rank #1 -> extend holding instead of
# exiting on max-holding-day" rule below. Kept identical to the backtest's
# ETF_UNIVERSE so live ranking matches backtest ranking exactly.
_SYMBOLS = """
ABSLBANETF ABSLNN50ET ABSLPSE ALPHA ALPHAETF ALPL30IETF AONEGOLD AONENIFTY AONESILVER AONETMMQ50
AONETOTAL AUTOBEES AUTOIETF BANK10ADD BANK10BETF BANKBEES BANKBETA BANKBETF BANKETF BANKIETF
BANKNIFTY1 BFSI BSE500IETF BSLGOLDETF BSLNIFTY CEMNTGROWW CHEMICAL COMMOIETF CONS CONSUMBEES
CONSUMER CONSUMIETF CPSEETF DEFENCE DIVOPPBEES ECAPINSURE EGOLD ELM250 EMETAL EMULTIMQ ENERGY
ENERGYAXIS ENIFTY EQUAL200 EQUAL50 EQUAL50ADD ESG ESILVER EVIETF EVINDIA FINIETF FLEXIADD FMCGADD
FMCGIETF GOLD1 GOLDADD GOLDAXIS GOLDBEES GOLDBETA GOLDBND GOLDCASE GOLDETF GOLDIETF GROWWCAPM
GROWWCHEM GROWWDEFNC GROWWEV GROWWGOLD GROWWHOSPI GROWWLOVOL GROWWMETAL GROWWMOM50 GROWWN200
GROWWNET GROWWNIFTY GROWWNXT50 GROWWPOWER GROWWRAIL GROWWRLTY GROWWSC250 GROWWSLVR HDFCBSE500
HDFCGOLD HDFCMID150 HDFCMOMENT HDFCNEXT50 HDFCNIF100 HDFCNIFBAN HDFCNIFIT HDFCNIFTY HDFCNIMEG
HDFCPSUBK HDFCPVTBAN HDFCSENSEX HDFCSILVER HDFCSML250 HEALTHADD HEALTHCARE HEALTHIETF HEALTHY
HNGSNGBEES HSBCGOLD ICICIB22 INFRA INFRAIETF INSUREIETF INTERNET IT ITBEES ITETF ITIETF IVZINGOLD
JUNIORBEES LICMFGOLD LICNMID100 LOWVOL1 LOWVOLIETF MAFANG MAHKTECH MAKEINDIA MASPTOP50 METAL
METALIETF MID150 MID150BEES MID150CASE MIDCAP MIDCAPADD MIDCAPETF MIDCAPIETF MIDQ50ADD MIDSELIETF
MIDSMALL MNC MOCAPITAL MODEFENCE MOENERGY MOGOLD MOHEALTH MOM100 MOM30IETF MOMENTUM MOMENTUM30
MOMENTUM50 MOMETAL MOMIDMTM MOMMIDCAP MOMOMENTUM MON100 MONIFTY500 MONQ50 MOOILGAS MOREALTY
MOSILVER MOSMALL250 MOTOUR MOVALUE MSCI360 MULTICAP NEXT50 NEXT50BETA NEXT50ETF NEXT50IETF
NIF100BEES NIF100IETF NIFTY1 NIFTY100EW NIFTYADD NIFTYAXIS NIFTYBEES NIFTYBETA NIFTYCASE NIFTYETF
NIFTYIETF NIFTYJBLK NIFTYQLITY NV20 NV20BEES NV20IETF OILIETF PHARMABEES PSUBANK PSUBANKADD
PSUBNKBEES PSUBNKIETF PVTBANIETF PVTBANKADD PVTBKGROWW QGOLDHALF QUAL30IETF QUALITY30 SBIBPB
SBIMIDMOM SBINEQWETF SBINMID150 SBISILVER SBISMLETF SBIVALETF SELECTIPO SENSEXETF SENSEXIETF
SETFGOLD SETFNIF50 SETFNIFBK SETFNN50 SHARIABEES SILVER SILVER001 SILVER1 SILVER360 SILVERADD
SILVERAG SILVERAXIS SILVERBEES SILVERBETA SILVERBND SILVERCASE SILVERIETF SMALL250 SMALLADD
SMALLCAP SMALLGROWW SMALLIETF SML100CASE SNXT30BEES TATAGOLD TATSILV TECH TNIDETF TOP100CASE
TOP10ADD TOP15IETF TOP20 TWCGOLDETF VAL30IETF VALUE VALUEAXIS
"""
ETF_UNIVERSE = [s + ".NS" for s in _SYMBOLS.split()]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "portfolio.json")
STATE_PATH = os.path.join(BASE_DIR, "docs", "state.json")
ETF_UNIVERSE_PATH = os.path.join(BASE_DIR, "etf_universe.txt")
HOLIDAYS_PATH = os.path.join(BASE_DIR, "nse_holidays.json")

REASON_LABELS = {
    "take_profit": "Target hit",
    "stop_loss": "Stop-loss hit",
    "mean_reversion": f"Back at {DMA_WINDOW} DMA",
    "ema_death_cross": f"EMA {EMA_FAST}/{EMA_SLOW} death cross",
    "max_holding_period": f"Held {MAX_HOLDING_DAYS} days",
    "extend": f"Still Rank #1 - holding extended",
    "trail_started": "Target hit, still Rank #1 - trailing started (do NOT sell)",
    "trail_stop": f"Trailing stop hit ({TRAIL_PCT:g}% below peak)",
    "trail_rank_lost": "Trailing - no longer Rank #1",
    "breakeven_stop": f"Breakeven stop hit (entry +{BREAKEVEN_STOP_PCT:g}%)",
    "late_hold_ema_exit": f"Late-hold EMA {LATE_HOLD_EMA_FAST}/{LATE_HOLD_EMA_SLOW} death cross",
    "buy_signal": "Buy signal (rank entry)",
}


# ==============================================================================
# MARKET CLOCK
# ==============================================================================

def now_ist():
    """Current IST time. SCAN_NOW_IST=2026-10-05T15:20:00 overrides it (testing only)."""
    o = os.environ.get("SCAN_NOW_IST", "").strip()
    if o:
        return datetime.fromisoformat(o).replace(tzinfo=IST)
    return datetime.now(IST)


def load_holidays():
    try:
        with open(HOLIDAYS_PATH, encoding="utf-8") as f:
            return {date.fromisoformat(x) for x in json.load(f).get("holidays", [])}
    except Exception as e:
        print(f"[clock] could not read nse_holidays.json ({e}) - holidays not checked")
        return set()


def is_trading_day(d, holidays):
    return d.weekday() < 5 and d not in holidays


# ==============================================================================
# STORAGE
# ==============================================================================

def blank_store():
    return {"positions": [], "alerts": [], "meta": {},
            "next_position_id": 1, "next_alert_id": 1}


def load_store():
    if not os.path.exists(DATA_PATH):
        return blank_store()
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        s = blank_store()
        s.update(loaded)
        s["next_position_id"] = max(
            [int(s["next_position_id"])] + [int(p["id"]) + 1 for p in s["positions"]])
        s["next_alert_id"] = max(
            [int(s["next_alert_id"])] + [int(a["id"]) + 1 for a in s["alerts"]])
        return s
    except Exception as e:
        backup = DATA_PATH + ".broken-" + datetime.now().strftime("%Y%m%d%H%M%S")
        print(f"[store] portfolio.json unreadable ({e}). Moved to {os.path.basename(backup)}.")
        try:
            shutil.move(DATA_PATH, backup)
        except Exception:
            pass
        return blank_store()


def save_store(s):
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, DATA_PATH)


def list_open_positions(s):
    return [p for p in s["positions"] if p["status"] == "OPEN"]


# ==============================================================================
# PRICE DATA
# ==============================================================================

def normalise_ticker(t):
    t = (t or "").strip().upper()
    if t.startswith("NSE:"):
        t = t[4:]
    if not t.endswith(".NS"):
        t = t + ".NS"
    return t


def fetch_live_quotes(tickers):
    import yfinance as yf
    out = {}
    for t in list(dict.fromkeys(tickers)):
        price, source, err = None, None, None
        try:
            tk = yf.Ticker(t)
            try:
                fi = tk.fast_info
                p = None
                for key in ("last_price", "lastPrice", "regularMarketPrice"):
                    try:
                        p = fi[key] if hasattr(fi, "__getitem__") else getattr(fi, key, None)
                    except (KeyError, AttributeError, TypeError):
                        p = getattr(fi, key, None)
                    if p:
                        break
                if p and float(p) > 0:
                    price, source = float(p), "live"
            except Exception as e:
                err = f"quote: {type(e).__name__}"

            if price is None:
                try:
                    intra = tk.history(period="1d", interval="1m")
                    if intra is not None and len(intra):
                        c = intra["Close"].dropna()
                        if len(c):
                            price, source, err = float(c.iloc[-1]), "intraday", None
                except Exception as e:
                    err = err or f"intraday: {type(e).__name__}"
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:120]}"
        out[t] = {"price": price, "source": source, "error": err}
    return out


def _slice_ticker(raw, t):
    """One ticker's High/Low/Close out of a yf.download() frame (handles both
    MultiIndex layouts yfinance has used)."""
    cols = raw.columns
    if isinstance(cols, pd.MultiIndex):
        if t in cols.get_level_values(0):
            sub = raw[t]
        elif t in cols.get_level_values(1):
            sub = raw.xs(t, axis=1, level=1)
        else:
            return None
    else:
        sub = raw
    keep = [c for c in ("High", "Low", "Close") if c in sub.columns]
    if "Close" not in keep:
        return None
    return sub[keep].copy()


def fetch_history(tickers):
    """Daily bars (High/Low/Close). The last row is TODAY's partial bar while the
    market is open, so its High/Low are the day's range so far."""
    import yfinance as yf
    tickers = list(dict.fromkeys(tickers))
    out, errors = {}, {}
    if not tickers:
        return out, errors

    raw, bulk_err = None, None
    try:
        raw = yf.download(tickers, period=PRICE_HISTORY_PERIOD, auto_adjust=True,
                          progress=False, group_by="ticker", threads=True)
    except Exception as e:
        bulk_err = f"{type(e).__name__}: {str(e)[:150]}"
        print(f"[prices] bulk download failed: {bulk_err}")

    for t in tickers:
        df, err = None, bulk_err
        try:
            if raw is None or len(raw) == 0:
                df = None
            else:
                df = _slice_ticker(raw, t)
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:150]}"
            df = None

        if df is None or not len(df.dropna(subset=["Close"])):
            try:
                single = yf.Ticker(t).history(period=PRICE_HISTORY_PERIOD)
                if single is not None and len(single):
                    df = single[[c for c in ("High", "Low", "Close") if c in single.columns]].copy()
                    err = None
            except Exception as e:
                err = err or f"{type(e).__name__}: {str(e)[:150]}"

        if df is not None:
            df = df.dropna(subset=["Close"])
            if len(df):
                idx = pd.to_datetime(df.index)
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_localize(None)
                df.index = idx.normalize()
                df = df[~df.index.duplicated(keep="last")]
                out[t] = df
                continue

        errors[t] = err or "Yahoo Finance returned no data for this symbol"

    return out, errors


def build_indicators(close: pd.Series):
    close = close.dropna()
    dma = close.rolling(window=DMA_WINDOW, min_periods=DMA_WINDOW).mean()
    pct_dist = (close - dma) / dma * 100
    ema_fast = close.ewm(span=EMA_FAST, min_periods=EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW, min_periods=EMA_SLOW, adjust=False).mean()
    late_fast = close.ewm(span=LATE_HOLD_EMA_FAST, min_periods=LATE_HOLD_EMA_FAST, adjust=False).mean()
    late_slow = close.ewm(span=LATE_HOLD_EMA_SLOW, min_periods=LATE_HOLD_EMA_SLOW, adjust=False).mean()

    def at(series, i):
        try:
            v = series.iloc[i]
            return None if pd.isna(v) else float(v)
        except (IndexError, KeyError):
            return None

    return {
        "close": at(close, -1),
        "close_date": close.index[-1].date().isoformat() if len(close) else None,
        "dma": at(dma, -1),
        "pct_dist": at(pct_dist, -1),
        "ema_fast_today": at(ema_fast, -1),
        "ema_slow_today": at(ema_slow, -1),
        "ema_fast_prev": at(ema_fast, -2),
        "ema_slow_prev": at(ema_slow, -2),
        "late_fast_today": at(late_fast, -1),
        "late_slow_today": at(late_slow, -1),
        "late_fast_prev": at(late_fast, -2),
        "late_slow_prev": at(late_slow, -2),
    }


EMPTY_IND = {"close": None, "close_date": None, "dma": None, "pct_dist": None,
             "ema_fast_today": None, "ema_slow_today": None,
             "ema_fast_prev": None, "ema_slow_prev": None,
             "late_fast_today": None, "late_slow_today": None,
             "late_fast_prev": None, "late_slow_prev": None}


# ==============================================================================
# UNIVERSE RANKING (only needed for the max-holding "still rank #1" check)
# ==============================================================================

def build_universe_ranking():
    """Fetch the whole ETF universe and rank tickers by %-distance below their
    DMA (most negative = most discounted = Rank #1), identical to the
    backtest's build_daily_rankings(). Returns a sorted list of
    (ticker, close, pct_dist) tuples, most-discounted first."""
    hist, errors = fetch_history(ETF_UNIVERSE)
    if errors:
        print(f"[universe] {len(errors)}/{len(ETF_UNIVERSE)} tickers failed to fetch")

    rows = []
    skipped = []
    ref_day = now_ist().date()
    for t, df in hist.items():
        close = df["Close"].dropna()
        if len(close) < DMA_WINDOW:
            continue
        # bad-print / stale guards (backtest drops >20% single-day prints and ffills max 5 days)
        if (ref_day - close.index[-1].date()).days > MAX_STALE_DAYS:
            skipped.append(f"{t} stale")
            continue
        if len(close) >= 2 and close.iloc[-2] > 0:
            last_move = abs(close.iloc[-1] / close.iloc[-2] - 1) * 100
            if last_move > MAX_DAILY_MOVE_PCT:
                skipped.append(f"{t} move {last_move:.0f}%")
                continue
        dma_today = close.rolling(window=DMA_WINDOW, min_periods=DMA_WINDOW).mean().iloc[-1]
        if pd.isna(dma_today) or dma_today == 0:
            continue
        cmp_today = float(close.iloc[-1])
        pct_dist = (cmp_today - dma_today) / dma_today * 100
        rows.append((t, cmp_today, float(pct_dist)))

    if skipped:
        print(f"[universe] skipped {len(skipped)} (stale/bad print): {', '.join(skipped[:8])}")
    rows.sort(key=lambda x: x[2])
    return rows


# ==============================================================================
# SLOTS + ENTRY SIGNALS  (mirrors the "fresh entries" block of backtest_nse_split.py)
# ==============================================================================

def _d(x):
    return date.fromisoformat(str(x)[:10])


def assign_slots(s):
    """Open positions without a slot (older entries, or added with 'Auto') get the
    lowest free slot number. Persisted in portfolio.json."""
    used = {int(p["slot"]) for p in list_open_positions(s) if p.get("slot")}
    for p in sorted(list_open_positions(s), key=lambda x: x["id"]):
        if p.get("slot"):
            continue
        free = [i for i in range(1, N_SLOTS + 1) if i not in used]
        if free:
            p["slot"] = free[0]
            used.add(free[0])
        else:
            p["slot"] = None      # more open positions than slots - not tracked per slot


def slot_last_exit(s, slot_id):
    ds = [_d(p["exit_date"]) for p in s["positions"]
          if p["status"] == "CLOSED" and p.get("slot") == slot_id and p.get("exit_date")]
    return max(ds) if ds else None


def slot_cash_estimate(s, slot_id):
    """Cash a slot would put into the next buy: proceeds of its last sale (compounding,
    like the backtest) or INITIAL_CAPITAL / N_SLOTS if the slot was never used."""
    closed = [p for p in s["positions"]
              if p["status"] == "CLOSED" and p.get("slot") == slot_id
              and p.get("exit_price") and p.get("exit_date")]
    if closed:
        last = max(closed, key=lambda p: (p["exit_date"], p["id"]))
        return float(last["qty"]) * float(last["exit_price"]) * (1 - TRANSACTION_COST_PCT / 100)
    return INITIAL_CAPITAL / N_SLOTS


def slot_overview(s, today, pending_exit_ids):
    out = []
    open_by_slot = {p.get("slot"): p for p in list_open_positions(s) if p.get("slot")}
    for sid in range(1, N_SLOTS + 1):
        p = open_by_slot.get(sid)
        if p is not None:
            status = "pending_sell" if p["id"] in pending_exit_ids else "invested"
            out.append({"id": sid, "status": status, "ticker": p["ticker"].replace(".NS", ""),
                        "position_id": p["id"], "cash_est": None})
            continue
        last = slot_last_exit(s, sid)
        status = "blocked_t1" if (SETTLE_T1 and last is not None and last >= today) else "free"
        out.append({"id": sid, "status": status, "ticker": None, "position_id": None,
                    "cash_est": slot_cash_estimate(s, sid)})
    return out


def compute_entry_signals(s, today, pending_exit_ids, rows):
    """Which ETF to buy in which slot TODAY (same rules as the backtest's entry block).

    pending_exit_ids = open positions that an exit rule has fired on today (they are
    treated as sold today: their slot is blocked by T+1 and their ETF is not 'held')."""
    if not rows:
        return []
    open_pos = list_open_positions(s)
    remaining = [p for p in open_pos if p["id"] not in pending_exit_ids]
    n_open = len(remaining)
    held = {p["ticker"] for p in remaining}
    used_slots = {int(p["slot"]) for p in open_pos if p.get("slot")}   # incl. pending sells
    order = [r[0] for r in rows]
    signals = []
    for sid in range(1, N_SLOTS + 1):
        if n_open >= MAX_CONCURRENT_POSITIONS:
            break
        if sid in used_slots:
            continue
        last = slot_last_exit(s, sid)
        if SETTLE_T1 and last is not None and last >= today:
            continue
        pick = next((r for r in rows if ALLOW_SAME_ETF or r[0] not in held), None)
        if pick is None:
            continue
        t, price, pdist = pick
        cash = slot_cash_estimate(s, sid)
        qty = int(cash / price * (1 - TRANSACTION_COST_PCT / 100))
        if qty <= 0:
            continue
        signals.append({"slot": sid, "ticker": t, "display_ticker": t.replace(".NS", ""),
                        "price": price, "pct_dist": pdist, "rank": order.index(t) + 1,
                        "qty": qty, "amount": qty * price, "date": today.isoformat()})
        held.add(t)
        n_open += 1
    return signals


# ==============================================================================
# EXIT ENGINE
# ==============================================================================

def _dates(df):
    return pd.Series(df.index.date, index=df.index)


def _cross_down(fast_today, slow_today, fast_prev, slow_prev):
    """Death cross exactly like the backtest: fast was >= slow yesterday, below today."""
    if None in (fast_today, slow_today, fast_prev, slow_prev):
        return False
    return fast_prev >= slow_prev and fast_today < slow_today


def check_exit(position, ind, bars, session, today, close_window, is_rank1_fn):
    """Same decision order as backtest_nse.py run_backtest().

    ind          indicators built from closes with the live price spliced in
    bars         daily High/Low/Close frame (last row = today's partial bar)
    session      the trading day being evaluated (today while the market is open)
    close_window True => close-based rules are evaluated too
    is_rank1_fn  lazy callable -> is this ETF Rank #1 across the universe right now
    """
    cmp_ = ind["close"]
    res = {"reason": None, "pnl_pct": None, "live_pnl_pct": None, "detail": "",
           "price": cmp_, "updates": {}, "notice": None,
           "info": {"tp_price": None, "sl_price": None, "be_price": None, "be_armed": False,
                    "trailing": bool(position.get("trailing")), "trail_stop": None,
                    "trail_since": position.get("trail_since")}}
    if cmp_ is None:
        res["detail"] = "no price"
        return res

    avg = float(position["avg_price"])
    live_pct = (cmp_ - avg) / avg * 100
    res["live_pnl_pct"] = res["pnl_pct"] = live_pct
    entry = date.fromisoformat(str(position["entry_date"])[:10])
    days_held = (today - entry).days
    start = date.fromisoformat(str(position.get("trade_start") or position["entry_date"])[:10])

    has_hl = bars is not None and "High" in bars.columns and "Low" in bars.columns
    hi_col, lo_col = ("High", "Low") if has_hl else ("Close", "Close")
    prior = cur = None
    if bars is not None and len(bars):
        d = _dates(bars)
        prior, cur = bars[d < session], bars[d == session]

    # today's range so far (daily bar + live quote)
    day_high = day_low = cmp_
    if cur is not None and len(cur):
        h, l = cur[hi_col].iloc[-1], cur[lo_col].iloc[-1]
        if pd.notna(h):
            day_high = max(day_high, float(h))
        if pd.notna(l):
            day_low = min(day_low, float(l))

    tp_price = avg * (1 + TAKE_PROFIT_PCT / 100) if TAKE_PROFIT_PCT is not None else None
    sl_price = avg * (1 + STOP_LOSS_PCT / 100) if STOP_LOSS_PCT is not None else None
    be_price = avg * (1 + BREAKEVEN_STOP_PCT / 100) if BREAKEVEN_ENABLED else None

    # breakeven armed = +trigger% touched on an EARLIER day (after the original entry day)
    be_armed = False
    if BREAKEVEN_ENABLED and prior is not None and len(prior):
        after = prior[_dates(prior) > start]
        if len(after):
            hmax = after[hi_col].max()
            be_armed = bool(pd.notna(hmax) and hmax >= avg * (1 + BREAKEVEN_TRIGGER_PCT / 100))

    info = res["info"]
    info.update(tp_price=tp_price, sl_price=sl_price, be_price=be_price, be_armed=be_armed)

    tp_touched = tp_price is not None and day_high >= tp_price
    reason, price, detail = None, cmp_, ""

    if position.get("trailing"):
        t_since = date.fromisoformat(str(position.get("trail_since") or session.isoformat())[:10])
        peak = None
        if prior is not None and len(prior):
            seg = prior[_dates(prior) >= t_since]
            if len(seg):
                peak = seg[hi_col].max()
        if peak is None or pd.isna(peak):
            peak = tp_price if tp_price is not None else avg
        peak = max(float(peak), day_high) if t_since >= session else float(peak)
        stop_price = peak * (1 - TRAIL_PCT / 100)
        if TRAIL_LOCK_TP and tp_price is not None:
            stop_price = max(stop_price, tp_price)
        info["trail_stop"] = stop_price
        started_today = t_since >= session      # backtest: stop checks begin the NEXT session
        if not started_today and day_low <= stop_price:
            reason, price = "trail_stop", stop_price
            detail = (f"low {day_low:.2f} <= trailing stop {stop_price:.2f} "
                      f"(peak {peak:.2f}, -{TRAIL_PCT:g}%); live price {cmp_:.2f}")
        elif not started_today and close_window and not is_rank1_fn():
            reason, price = "trail_rank_lost", cmp_
            detail = "ETF is no longer Rank #1 while trailing - exit near close"
    elif tp_touched:
        if TRAIL_ENABLED and is_rank1_fn():
            res["updates"] = {"trailing": True, "trail_since": session.isoformat()}
            info["trailing"], info["trail_since"] = True, session.isoformat()
            ts = day_high * (1 - TRAIL_PCT / 100)
            if TRAIL_LOCK_TP:
                ts = max(ts, tp_price)
            info["trail_stop"] = ts
            res["notice"] = ("trail_started",
                             f"target {tp_price:.2f} touched (high {day_high:.2f}) and still Rank #1 - "
                             f"do NOT sell. Trailing stop starts next session at peak -{TRAIL_PCT:g}% "
                             f"(never below {tp_price:.2f}).", tp_price)
        else:
            reason, price = "take_profit", tp_price
            detail = (f"target {tp_price:.2f} (+{TAKE_PROFIT_PCT:g}%) touched, day high {day_high:.2f}; "
                      f"live price {cmp_:.2f}")
    elif be_armed and day_low <= be_price:
        reason, price = "breakeven_stop", be_price
        detail = (f"+{BREAKEVEN_TRIGGER_PCT:g}% was touched earlier; low {day_low:.2f} <= "
                  f"breakeven stop {be_price:.2f}; live price {cmp_:.2f}")
    elif sl_price is not None and day_low <= sl_price:
        reason, price = "stop_loss", sl_price
        detail = f"low {day_low:.2f} <= stop {sl_price:.2f} ({STOP_LOSS_PCT:g}%); live price {cmp_:.2f}"
    elif close_window and EXIT_ON_MEAN_REVERSION and ind["pct_dist"] is not None and ind["pct_dist"] >= 0:
        reason, price = "mean_reversion", cmp_
        detail = f"price back at/above {DMA_WINDOW}DMA ({ind['pct_dist']:+.2f}%)"
    elif (close_window and EXIT_ON_EMA_DEATH_CROSS
          and _cross_down(ind["ema_fast_today"], ind["ema_slow_today"],
                          ind["ema_fast_prev"], ind["ema_slow_prev"])):
        reason, price = "ema_death_cross", cmp_
        detail = (f"EMA{EMA_FAST} crossed below EMA{EMA_SLOW} "
                  f"({ind['ema_fast_today']:.2f} vs {ind['ema_slow_today']:.2f})")
    elif (close_window and LATE_HOLD_EMA_EXIT and days_held >= LATE_HOLD_START_DAY
          and _cross_down(ind["late_fast_today"], ind["late_slow_today"],
                          ind["late_fast_prev"], ind["late_slow_prev"])):
        reason, price = "late_hold_ema_exit", cmp_
        detail = (f"held {days_held} days (>= {LATE_HOLD_START_DAY}); fast EMA{LATE_HOLD_EMA_FAST} crossed "
                  f"below EMA{LATE_HOLD_EMA_SLOW} ({ind['late_fast_today']:.2f} vs {ind['late_slow_today']:.2f})")

    if reason is None and close_window and days_held >= MAX_HOLDING_DAYS:
        if is_rank1_fn():
            reason, price = "extend", cmp_
            detail = (f"held {days_held} days but still Rank #1 across the ETF universe - "
                      f"holding period reset")
        else:
            reason, price = "max_holding_period", cmp_
            detail = f"held {days_held} days, limit is {MAX_HOLDING_DAYS}"

    res.update(reason=reason, price=price, detail=detail)
    if reason and reason != "extend":
        res["pnl_pct"] = (price - avg) / avg * 100
    return res


# ==============================================================================
# TELEGRAM
# ==============================================================================

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "Telegram secrets not set"
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.status_code == 200:
            return True, "sent"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def _esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def alert_message(ticker, reason, price, pnl_pct, detail, kind="SELL"):
    label = REASON_LABELS.get(reason, reason)
    return (
        f"<b>{kind} — {ticker.replace('.NS', '')}</b>\n"
        f"{label}\n\n"
        f"Price: ₹{price:,.2f}\n"
        f"P&amp;L vs avg buy: {pnl_pct:+.2f}%\n"
        f"Why: {_esc(detail)}"
    )


def buy_message(ticker, slot, price, qty, amount, rank, pct_dist, detail=""):
    return (
        f"<b>BUY — {ticker.replace('.NS', '')}</b>  (Slot {slot})\n"
        f"Rank #{rank}, {pct_dist:+.2f}% vs {DMA_WINDOW}DMA\n\n"
        f"Price: ~₹{price:,.2f}\n"
        f"Suggested: {qty} units (~₹{amount:,.0f})\n"
        f"Place the order before 3:30 PM - the backtest buys at the close.\n"
        + (f"Note: {_esc(detail)}" if detail else "")
    ).rstrip()


# ==============================================================================
# TEST ALERTS - manually verify Telegram + phone notifications end-to-end
# without touching any real position. Triggered by the dashboard's "Send
# test alert" button (workflow_dispatch input TEST_ALERT), never by the
# scheduled cron runs.
# ==============================================================================

TEST_ALERT_SAMPLES = {
    "take_profit": (TAKE_PROFIT_PCT + 0.2, f"+{TAKE_PROFIT_PCT + 0.2:.2f}% vs avg buy, target was +{TAKE_PROFIT_PCT:g}%"),
    "stop_loss": (STOP_LOSS_PCT - 0.3, f"low 91.70 <= stop 92.00 ({STOP_LOSS_PCT:g}%); live price 91.70"),
    "trail_stop": (3.10, f"low 103.10 <= trailing stop 103.20 (peak 103.70, -{TRAIL_PCT:g}%); live price 103.10"),
    "breakeven_stop": (float(BREAKEVEN_STOP_PCT), f"+{BREAKEVEN_TRIGGER_PCT:g}% was touched earlier; low 100.90 <= breakeven stop {100 + BREAKEVEN_STOP_PCT:.2f}; live price 100.90"),
    "trail_rank_lost": (5.20, "ETF is no longer Rank #1 while trailing - exit near close"),
    "mean_reversion": (1.10, f"price back at/above {DMA_WINDOW}DMA (+1.10%)"),
    "ema_death_cross": (-2.50, f"EMA{EMA_FAST} crossed below EMA{EMA_SLOW} (98.40 vs 99.10)"),
    "max_holding_period": (-1.80, f"held {MAX_HOLDING_DAYS} days, limit is {MAX_HOLDING_DAYS}"),
    "late_hold_ema_exit": (0.60, f"held {LATE_HOLD_START_DAY + 1} days (>= {LATE_HOLD_START_DAY}); fast EMA{LATE_HOLD_EMA_FAST} crossed below EMA{LATE_HOLD_EMA_SLOW} (99.40 vs 99.55)"),
    "buy_signal": (None, "Slot 2: Rank #1 (-3.20% below DMA). Suggested qty 120 (~Rs 12,000)"),
}


def run_test_alert(s, reason):
    """Append one fake alert (ticker TEST, position_id -1) and send it
    through the exact same send_telegram()/alert_message() path a real exit
    would use. Never touches portfolio.json positions."""
    if reason not in TEST_ALERT_SAMPLES:
        print(f"[test-alert] unknown reason '{reason}' - skipping, valid: "
              f"{list(TEST_ALERT_SAMPLES)}")
        return
    pnl_pct, detail = TEST_ALERT_SAMPLES[reason]
    detail = "[TEST - ignore] " + detail
    price = 100.0
    if reason == "buy_signal":
        ok, err = send_telegram(buy_message("TEST.NS", 2, price, 120, 12000.0, 1, -3.2, detail))
    else:
        ok, err = send_telegram(alert_message("TEST.NS", reason, price, pnl_pct, detail))
    alert_id = s["next_alert_id"]
    s["next_alert_id"] += 1
    s["alerts"].append({
        "id": alert_id, "position_id": -1, "ticker": "TEST.NS",
        "reason": reason, "price": price, "pnl_pct": pnl_pct, "detail": detail,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "seen": False, "telegram_ok": ok,
    })
    print(f"[test-alert] reason={reason} telegram_ok={ok} telegram_err={err}")


# ==============================================================================
# MAIN RUN
# ==============================================================================

def raise_alert(s, p, reason, price, pnl_pct, detail, kind="SELL"):
    """One Telegram alert + dashboard alert per (position, reason)."""
    if any(a["position_id"] == p["id"] and a["reason"] == reason for a in s["alerts"]):
        return False
    alert_id = s["next_alert_id"]
    s["next_alert_id"] += 1
    ok, _ = send_telegram(alert_message(p["ticker"], reason, price, pnl_pct, detail, kind))
    s["alerts"].append({
        "id": alert_id, "position_id": p["id"], "ticker": p["ticker"],
        "reason": reason, "price": price, "pnl_pct": pnl_pct, "detail": detail,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "seen": False, "telegram_ok": ok,
    })
    return True


def raise_buy_alert(s, sig):
    """One Telegram + dashboard BUY alert per (day, slot, ticker). If the ranking changes
    later in the entry window and a different ETF becomes the pick, a new alert is sent."""
    key = f"{sig['date']}|{sig['slot']}|{sig['ticker']}"
    if any(a.get("key") == key for a in s["alerts"]):
        return False
    detail = (f"Slot {sig['slot']}: Rank #{sig['rank']} ({sig['pct_dist']:+.2f}% vs {DMA_WINDOW}DMA). "
              f"Suggested qty {sig['qty']} (~₹{sig['amount']:,.0f})")
    ok, _ = send_telegram(buy_message(sig["ticker"], sig["slot"], sig["price"], sig["qty"],
                                      sig["amount"], sig["rank"], sig["pct_dist"]))
    alert_id = s["next_alert_id"]
    s["next_alert_id"] += 1
    s["alerts"].append({
        "id": alert_id, "position_id": -2, "ticker": sig["ticker"], "key": key,
        "reason": "buy_signal", "price": sig["price"], "pnl_pct": None, "detail": detail,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "seen": False, "telegram_ok": ok,
    })
    return True


def main():
    now = now_ist()
    today = now.date()
    holidays = load_holidays()
    trading_today = is_trading_day(today, holidays)
    scheduled = os.environ.get("GITHUB_EVENT_NAME", "") == "schedule"

    # Scheduled runs only act inside the market window on trading days
    # (cron cannot know NSE holidays). Manual runs ("Check now") always scan.
    if scheduled and not (trading_today and SCHEDULE_GATE[0] <= now.time() <= SCHEDULE_GATE[1]):
        print(f"[scan] {now:%Y-%m-%d %H:%M} IST - market closed / holiday, nothing to do.")
        return

    # Close-based rules (DMA, EMA, rank, holding days) use the daily CLOSE in the backtest,
    # so they are evaluated from CLOSE_RULES_FROM (or when the market is not trading).
    trading_started = trading_today and now.time() >= MARKET_OPEN
    close_window = (not trading_started) or now.time() >= CLOSE_RULES_FROM
    print(f"[scan] {now:%Y-%m-%d %H:%M} IST  trading_day={trading_today}  "
          f"close_rules={'ON' if close_window else 'off (before ' + CLOSE_RULES_FROM.strftime('%H:%M') + ')'}")

    s = load_store()
    assign_slots(s)

    test_reason = os.environ.get("TEST_ALERT", "").strip()
    if test_reason and test_reason != "none":
        run_test_alert(s, test_reason)

    open_positions = list_open_positions(s)
    tickers = [p["ticker"] for p in open_positions]

    hist, errors = fetch_history(tickers)
    live = fetch_live_quotes(tickers) if (tickers and USE_LIVE_PRICE) else {}

    # Universe ranking is expensive (~250 tickers) - fetched lazily, only when a rule needs it
    rank_cache = {}

    def is_rank1_for(ticker):
        if "rows" not in rank_cache:
            rank_cache["rows"] = build_universe_ranking()
        rows = rank_cache["rows"]
        return bool(rows) and rows[0][0] == ticker

    open_rows, invested, current_value = [], 0.0, 0.0
    priced, unpriced = 0, 0
    new_alerts = 0

    for p in open_positions:
        p.setdefault("trade_start", p["entry_date"])
        df = hist.get(p["ticker"])

        # session = the trading day being evaluated
        if trading_started:
            session = today
        elif df is not None and len(df):
            session = df.index[-1].date()
        else:
            session = today

        q = live.get(p["ticker"]) or {}
        live_price = q.get("price")

        if df is not None and len(df):
            closes = df["Close"].astype(float).copy()
            if live_price:     # splice the live quote in as this session's close
                closes.loc[pd.Timestamp(session)] = float(live_price)
                closes = closes.sort_index()
            ind = build_indicators(closes)
        else:
            ind = dict(EMPTY_IND)

        if live_price:
            cmp_, price_source = float(live_price), q.get("source") or "live"
        elif ind["close"] is not None:
            cmp_, price_source = ind["close"], "close"
        else:
            cmp_, price_source = None, None

        price_error = None
        if cmp_ is None:
            price_error = errors.get(p["ticker"]) or q.get("error") or "No price found for this symbol on Yahoo Finance"

        ind_for_exit = dict(ind, close=cmp_)
        res = check_exit(p, ind_for_exit, df, session, today, close_window,
                         lambda t=p["ticker"]: is_rank1_for(t))
        reason, detail = res["reason"], res["detail"]
        p.update(res["updates"])

        # Still Rank #1 at the max-holding boundary - reset the holding clock instead of
        # exiting, exactly like the backtest's EXTEND. No sell, no alert.
        if reason == "extend":
            p.setdefault("extensions", []).append(today.isoformat())
            p["entry_date"] = today.isoformat()
            reason, detail = None, None

        cost = float(p["qty"]) * float(p["avg_price"])
        invested += cost
        if cmp_ is None:
            value = None
            unpriced += 1
        else:
            value = float(p["qty"]) * cmp_
            current_value += value
            priced += 1

        entry = datetime.fromisoformat(p["entry_date"]).date()
        days_held = (today - entry).days
        info = res["info"]

        open_rows.append({
            "id": p["id"], "ticker": p["ticker"], "slot": p.get("slot"),
            "display_ticker": p["ticker"].replace(".NS", ""),
            "entry_date": p["entry_date"], "qty": float(p["qty"]),
            "avg_price": float(p["avg_price"]), "note": p.get("note"),
            "cmp": cmp_, "price_source": price_source, "price_error": price_error,
            "price_date": ind["close_date"], "dma": ind["dma"], "pct_dist": ind["pct_dist"],
            "ema_fast": ind["ema_fast_today"], "ema_slow": ind["ema_slow_today"],
            "invested": cost, "value": value,
            "pnl": (value - cost) if value is not None else None,
            "pnl_pct": res["live_pnl_pct"], "days_held": days_held,
            "days_left": max(MAX_HOLDING_DAYS - days_held, 0),
            "tp_price": info["tp_price"], "sl_price": info["sl_price"],
            "be_price": info["be_price"], "be_armed": info["be_armed"],
            "trailing": bool(p.get("trailing")), "trail_stop": info["trail_stop"],
            "trail_since": p.get("trail_since"),
            "exit_reason": reason,
            "exit_label": REASON_LABELS.get(reason) if reason else None,
            "exit_detail": detail if reason else None,
        })

        if res["notice"]:                                   # e.g. trailing started (HOLD, not SELL)
            nr, ndetail, nprice = res["notice"]
            if raise_alert(s, p, nr, nprice, (nprice - float(p["avg_price"])) / float(p["avg_price"]) * 100,
                           ndetail, kind="HOLD"):
                new_alerts += 1
        if reason:
            if raise_alert(s, p, reason, res["price"], res["pnl_pct"], detail):
                new_alerts += 1

    # ---- fresh-entry (BUY) signals - same decision as the backtest's entry block ----
    pending_exit_ids = {r["id"] for r in open_rows if r["exit_reason"]}
    entry_window = (trading_today and ENTRY_SIGNAL_FROM <= now.time() <= ENTRY_SIGNAL_UNTIL)
    today_iso = today.isoformat()
    meta_sig = s["meta"].get("entry_signals") or {}
    if entry_window:
        free_exists = any(sl["status"] == "free" for sl in slot_overview(s, today, pending_exit_ids))
        need_buy = (len(open_rows) - len(pending_exit_ids)) < MAX_CONCURRENT_POSITIONS and free_exists
        signals = []
        if need_buy:
            rows = rank_cache["rows"] if "rows" in rank_cache else build_universe_ranking()
            rank_cache["rows"] = rows
            signals = compute_entry_signals(s, today, pending_exit_ids, rows)
            if not signals:
                print("[entry] slot free but no ETF could be ranked - no signal")
        for sig in signals:
            if raise_buy_alert(s, sig):
                new_alerts += 1
        s["meta"]["entry_signals"] = {"date": today_iso, "signals": signals}
    elif meta_sig.get("date") != today_iso:
        s["meta"]["entry_signals"] = {"date": today_iso, "signals": []}
    entry_signals = (s["meta"].get("entry_signals") or {}).get("signals", [])
    # a signal is stale once its slot got a position (user already bought)
    taken = {int(p["slot"]) for p in list_open_positions(s) if p.get("slot")}
    entry_signals = [g for g in entry_signals if g["slot"] not in taken and g["date"] == today_iso]
    slots = slot_overview(s, today, pending_exit_ids)

    closed_rows = sorted(
        [p for p in s["positions"] if p["status"] == "CLOSED"],
        key=lambda p: (p.get("exit_date") or "", p["id"]), reverse=True)[:50]
    closed = [{
        "id": p["id"], "display_ticker": p["ticker"].replace(".NS", ""), "slot": p.get("slot"),
        "entry_date": p["entry_date"], "exit_date": p.get("exit_date"),
        "exit_price": p.get("exit_price"), "exit_reason": p.get("exit_reason"),
        "exit_label": REASON_LABELS.get(p.get("exit_reason"), p.get("exit_reason")),
        "pnl_pct": p.get("exit_pnl_pct"),
    } for p in closed_rows]

    alert_rows = sorted(s["alerts"], key=lambda a: (a["created_at"], a["id"]), reverse=True)[:50]
    alerts = [{
        "id": a["id"], "position_id": a["position_id"],
        "display_ticker": a["ticker"].replace(".NS", ""), "reason": a["reason"],
        "label": REASON_LABELS.get(a["reason"], a["reason"]), "price": a["price"],
        "pnl_pct": a["pnl_pct"], "detail": a["detail"], "created_at": a["created_at"],
        "seen": bool(a["seen"]), "telegram_ok": bool(a["telegram_ok"]),
    } for a in alert_rows]

    s["meta"]["last_scan"] = datetime.now().isoformat(timespec="seconds")
    save_store(s)

    state = {
        "open_positions": sorted(open_rows, key=lambda p: (p["entry_date"], p["id"]), reverse=True),
        "closed_positions": closed,
        "alerts": alerts,
        "summary": {
            "invested": invested,
            "priced_invested": sum(p["invested"] for p in open_rows if p["value"] is not None),
            "value": current_value,
            "pnl": current_value - sum(p["invested"] for p in open_rows if p["value"] is not None),
            "pnl_pct": None,
            "open_count": len(open_rows), "priced_count": priced, "unpriced_count": unpriced,
            "action_count": sum(1 for p in open_rows if p["exit_reason"]),
        },
        "slots": slots,
        "entry_signals": entry_signals,
        "rules": {
            "dma_window": DMA_WINDOW, "n_slots": N_SLOTS,
            "max_concurrent": MAX_CONCURRENT_POSITIONS, "settle_t1": SETTLE_T1,
            "allow_same_etf": ALLOW_SAME_ETF, "initial_capital": INITIAL_CAPITAL,
            "entry_signal_from": ENTRY_SIGNAL_FROM.strftime("%H:%M"),
            "take_profit_pct": TAKE_PROFIT_PCT, "stop_loss_pct": STOP_LOSS_PCT,
            "max_holding_days": MAX_HOLDING_DAYS, "ema_fast": EMA_FAST, "ema_slow": EMA_SLOW,
            "ema_exit_on": EXIT_ON_EMA_DEATH_CROSS, "mean_reversion_on": EXIT_ON_MEAN_REVERSION,
            "trail_enabled": TRAIL_ENABLED, "trail_pct": TRAIL_PCT, "trail_lock_tp": TRAIL_LOCK_TP,
            "breakeven_enabled": BREAKEVEN_ENABLED,
            "breakeven_trigger_pct": BREAKEVEN_TRIGGER_PCT, "breakeven_stop_pct": BREAKEVEN_STOP_PCT,
            "late_hold_on": LATE_HOLD_EMA_EXIT, "late_hold_start_day": LATE_HOLD_START_DAY,
            "late_hold_ema_fast": LATE_HOLD_EMA_FAST, "late_hold_ema_slow": LATE_HOLD_EMA_SLOW,
            "close_rules_from": CLOSE_RULES_FROM.strftime("%H:%M"),
        },
        "last_scan": s["meta"]["last_scan"],
        "telegram_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }
    pc = sum(p["invested"] for p in open_rows if p["value"] is not None)
    state["summary"]["pnl_pct"] = ((current_value - pc) / pc * 100) if pc else None

    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)

    print(f"[scan] checked={len(open_rows)} new_alerts={new_alerts} unpriced={unpriced} "
          f"buy_signals={len(entry_signals)}")


if __name__ == "__main__":
    main()

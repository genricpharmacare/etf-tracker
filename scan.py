"""
==============================================================================
ETF Ki Dukan - Portfolio Tracker  (GitHub Actions edition)
==============================================================================
This is the same tracker, minus Termux and Flask. Instead of a phone-hosted
server it is designed to be run ONCE PER INVOCATION by a GitHub Actions
scheduled workflow (see .github/workflows/scan.yml):

  1. Reads positions from portfolio.json (in the repo).
  2. Fetches prices, runs the exit engine (identical logic to the original
     Termux version), sends any new Telegram alerts.
  3. Writes the updated portfolio.json AND docs/state.json - the second file
     is what the static dashboard (hosted on GitHub Pages, in docs/) reads
     to render the UI. The workflow then commits both files back to the repo.

Telegram credentials come from environment variables (GitHub Secrets), not
from a config.json on a phone:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

EXIT ENGINE FIDELITY - unchanged from the original:
    if   TAKE_PROFIT_PCT is not None and pct_vs_avg >= TAKE_PROFIT_PCT
    elif STOP_LOSS_PCT   is not None and pct_vs_avg <= STOP_LOSS_PCT
    elif EXIT_ON_MEAN_REVERSION      and pct_dist_today >= 0
    elif EXIT_ON_EMA_DEATH_CROSS     and (fast_prev >= slow_prev and fast_today < slow_today)
    if exit_reason is None and (today - entry_date).days >= MAX_HOLDING_DAYS
==============================================================================
"""

import os
import json
import shutil
import time
from datetime import datetime, date

import pandas as pd

# ==============================================================================
# STRATEGY CONFIG - keep identical to your backtest script
# ==============================================================================

DMA_WINDOW = 20
TAKE_PROFIT_PCT = 4.0
STOP_LOSS_PCT = -10
EXIT_ON_MEAN_REVERSION = True
MAX_HOLDING_DAYS = 10

EXIT_ON_EMA_DEATH_CROSS = True
EMA_FAST = 20
EMA_SLOW = 50

USE_LIVE_PRICE = True
USE_MASTER_CALENDAR = False
PRICE_HISTORY_PERIOD = "6mo"

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

REASON_LABELS = {
    "take_profit": "Target hit",
    "stop_loss": "Stop-loss hit",
    "mean_reversion": "Back at 20 DMA",
    "ema_death_cross": f"EMA {EMA_FAST}/{EMA_SLOW} death cross",
    "max_holding_period": f"Held {MAX_HOLDING_DAYS} days",
    "extend": f"Still Rank #1 - holding extended",
}


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


def fetch_history(tickers):
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
            elif len(tickers) == 1:
                df = raw[["Close"]].copy()
            else:
                df = raw[t][["Close"]].copy()
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:150]}"
            df = None

        if df is None or not len(df.dropna()):
            try:
                single = yf.Ticker(t).history(period=PRICE_HISTORY_PERIOD)
                if single is not None and len(single):
                    df = single[["Close"]].copy()
                    err = None
            except Exception as e:
                err = err or f"{type(e).__name__}: {str(e)[:150]}"

        if df is not None:
            df = df.dropna()
            if len(df):
                df.index = pd.to_datetime(df.index)
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
    }


EMPTY_IND = {"close": None, "close_date": None, "dma": None, "pct_dist": None,
             "ema_fast_today": None, "ema_slow_today": None,
             "ema_fast_prev": None, "ema_slow_prev": None}


# ==============================================================================
# UNIVERSE RANKING (only needed for the max-holding "still rank #1" check)
# ==============================================================================

def build_universe_ranking():
    """Fetch the whole ETF universe and rank tickers by %-distance below their
    20DMA (most negative = most discounted = Rank #1), identical to the
    backtest's build_daily_rankings(). Returns a sorted list of
    (ticker, close, pct_dist) tuples, most-discounted first."""
    hist, errors = fetch_history(ETF_UNIVERSE)
    if errors:
        print(f"[universe] {len(errors)}/{len(ETF_UNIVERSE)} tickers failed to fetch")

    rows = []
    for t, df in hist.items():
        close = df["Close"].dropna()
        if len(close) < DMA_WINDOW:
            continue
        dma_today = close.rolling(window=DMA_WINDOW, min_periods=DMA_WINDOW).mean().iloc[-1]
        if pd.isna(dma_today) or dma_today == 0:
            continue
        cmp_today = float(close.iloc[-1])
        pct_dist = (cmp_today - dma_today) / dma_today * 100
        rows.append((t, cmp_today, float(pct_dist)))

    rows.sort(key=lambda x: x[2])
    return rows


# ==============================================================================
# EXIT ENGINE
# ==============================================================================

def check_exit(position, ind, today=None, is_rank1=False):
    today = today or date.today()
    cmp_ = ind["close"]
    if cmp_ is None:
        return None, None, "no price"

    avg = float(position["avg_price"])
    pct_vs_avg = (cmp_ - avg) / avg * 100
    entry = datetime.fromisoformat(position["entry_date"]).date()
    days_held = (today - entry).days

    exit_reason, detail = None, ""

    if TAKE_PROFIT_PCT is not None and pct_vs_avg >= TAKE_PROFIT_PCT:
        exit_reason = "take_profit"
        detail = f"{pct_vs_avg:+.2f}% vs avg buy, target was +{TAKE_PROFIT_PCT:.1f}%"
    elif STOP_LOSS_PCT is not None and pct_vs_avg <= STOP_LOSS_PCT:
        exit_reason = "stop_loss"
        detail = f"{pct_vs_avg:+.2f}% vs avg buy, stop was {STOP_LOSS_PCT:.1f}%"
    elif (EXIT_ON_MEAN_REVERSION and ind["pct_dist"] is not None and ind["pct_dist"] >= 0):
        exit_reason = "mean_reversion"
        detail = f"price back at/above 20DMA ({ind['pct_dist']:+.2f}%)"
    elif EXIT_ON_EMA_DEATH_CROSS:
        ft, st = ind["ema_fast_today"], ind["ema_slow_today"]
        fp, sp = ind["ema_fast_prev"], ind["ema_slow_prev"]
        if None not in (ft, st, fp, sp) and fp >= sp and ft < st:
            exit_reason = "ema_death_cross"
            detail = f"EMA{EMA_FAST} crossed below EMA{EMA_SLOW} ({ft:.2f} vs {st:.2f})"

    if exit_reason is None and days_held >= MAX_HOLDING_DAYS:
        if is_rank1:
            # Same ETF is still the #1 pick (most below its 20DMA) across the
            # whole universe - no point selling and immediately re-buying the
            # same thing. Reset the holding clock instead of exiting, exactly
            # like the backtest's EXTEND behaviour.
            exit_reason = "extend"
            detail = (f"held {days_held} days but still Rank #1 across the "
                      f"ETF universe - holding period reset")
        else:
            exit_reason = "max_holding_period"
            detail = f"held {days_held} days, limit is {MAX_HOLDING_DAYS}"

    return exit_reason, pct_vs_avg, detail


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


def alert_message(ticker, reason, price, pnl_pct, detail):
    label = REASON_LABELS.get(reason, reason)
    return (
        f"<b>SELL — {ticker.replace('.NS', '')}</b>\n"
        f"{label}\n\n"
        f"Price: ₹{price:,.2f}\n"
        f"P&amp;L vs avg buy: {pnl_pct:+.2f}%\n"
        f"Why: {detail}"
    )


# ==============================================================================
# MAIN RUN
# ==============================================================================

def main():
    s = load_store()
    open_positions = list_open_positions(s)
    tickers = [p["ticker"] for p in open_positions]

    hist, errors = fetch_history(tickers)
    live = fetch_live_quotes(tickers) if (tickers and USE_LIVE_PRICE) else {}

    today = date.today()
    open_rows, invested, current_value = [], 0.0, 0.0
    priced, unpriced = 0, 0
    new_alerts = 0

    # Only fetch/rank the whole ETF universe when at least one open position
    # is at (or past) the max-holding boundary today - that's the only place
    # the rank-#1 check matters, and this avoids an expensive ~250-ticker
    # fetch on every single run.
    need_rank_check = any(
        (today - datetime.fromisoformat(p["entry_date"]).date()).days >= MAX_HOLDING_DAYS
        for p in open_positions
    )
    rank1_ticker = None
    if need_rank_check:
        universe_rank = build_universe_ranking()
        if universe_rank:
            rank1_ticker = universe_rank[0][0]

    for p in open_positions:
        df = hist.get(p["ticker"])
        ind = build_indicators(df["Close"]) if df is not None and len(df) else dict(EMPTY_IND)

        q = live.get(p["ticker"]) or {}
        if q.get("price"):
            cmp_, price_source = q["price"], q.get("source") or "live"
        elif ind["close"] is not None:
            cmp_, price_source = ind["close"], "close"
        else:
            cmp_, price_source = None, None

        price_error = None
        if cmp_ is None:
            price_error = errors.get(p["ticker"]) or q.get("error") or "No price found for this symbol on Yahoo Finance"

        ind_for_exit = dict(ind, close=cmp_)
        is_rank1 = bool(rank1_ticker) and p["ticker"] == rank1_ticker
        reason, pnl_pct, detail = check_exit(p, ind_for_exit, today, is_rank1)

        # Still Rank #1 at the max-holding boundary - reset the holding clock
        # instead of exiting, exactly like the backtest's EXTEND behaviour.
        # No sell, no Telegram alert - just a fresh entry_date.
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

        open_rows.append({
            "id": p["id"], "ticker": p["ticker"],
            "display_ticker": p["ticker"].replace(".NS", ""),
            "entry_date": p["entry_date"], "qty": float(p["qty"]),
            "avg_price": float(p["avg_price"]), "note": p.get("note"),
            "cmp": cmp_, "price_source": price_source, "price_error": price_error,
            "price_date": ind["close_date"], "dma": ind["dma"], "pct_dist": ind["pct_dist"],
            "ema_fast": ind["ema_fast_today"], "ema_slow": ind["ema_slow_today"],
            "invested": cost, "value": value,
            "pnl": (value - cost) if value is not None else None,
            "pnl_pct": pnl_pct, "days_held": days_held,
            "days_left": max(MAX_HOLDING_DAYS - days_held, 0),
            "exit_reason": reason,
            "exit_label": REASON_LABELS.get(reason) if reason else None,
            "exit_detail": detail if reason else None,
        })

        # Raise a Telegram alert once per (position, reason)
        if reason:
            already = any(a["position_id"] == p["id"] and a["reason"] == reason for a in s["alerts"])
            if not already:
                alert_id = s["next_alert_id"]
                s["next_alert_id"] += 1
                ok, _ = send_telegram(alert_message(p["ticker"], reason, cmp_, pnl_pct, detail))
                s["alerts"].append({
                    "id": alert_id, "position_id": p["id"], "ticker": p["ticker"],
                    "reason": reason, "price": cmp_, "pnl_pct": pnl_pct, "detail": detail,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "seen": False, "telegram_ok": ok,
                })
                new_alerts += 1

    closed_rows = sorted(
        [p for p in s["positions"] if p["status"] == "CLOSED"],
        key=lambda p: (p.get("exit_date") or "", p["id"]), reverse=True)[:50]
    closed = [{
        "id": p["id"], "display_ticker": p["ticker"].replace(".NS", ""),
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
        "rules": {
            "take_profit_pct": TAKE_PROFIT_PCT, "stop_loss_pct": STOP_LOSS_PCT,
            "max_holding_days": MAX_HOLDING_DAYS, "ema_fast": EMA_FAST, "ema_slow": EMA_SLOW,
            "ema_exit_on": EXIT_ON_EMA_DEATH_CROSS, "mean_reversion_on": EXIT_ON_MEAN_REVERSION,
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

    print(f"[scan] checked={len(open_rows)} new_alerts={new_alerts} unpriced={unpriced}")


if __name__ == "__main__":
    main()

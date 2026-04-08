"""
OneMP11 Alert Server V2.1 — Synced with V8.1b
Database-driven Claude analysis. No external market data dependencies.
Deploy on Railway: https://railway.app
"""

import os
import json
import re
import sqlite3
import time
import traceback
import threading
import feedparser
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ===================================================
# CONFIG
# ===================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
WEBHOOK_SECRET      = os.environ.get("WEBHOOK_SECRET", "onemp11")
ENABLE_CLAUDE       = os.environ.get("ENABLE_CLAUDE", "true").lower() == "true"
DB_PATH             = os.environ.get("DB_PATH", "alerts.db")


# ===================================================
# FINANCIALJUICE NEWS FEED CONFIG
# ===================================================
FJ_RSS_URL = "https://www.financialjuice.com/feed.ashx?xy=rss"
FJ_POLL_INTERVAL = 30  # seconds between polls
FJ_MAX_HEADLINES = 20  # max headlines to cache
ENABLE_NEWS = os.environ.get("ENABLE_NEWS", "true").lower() == "true"

# In-memory news cache (thread-safe)
news_cache = []
news_cache_lock = threading.Lock()
news_last_poll = 0

# ===================================================
# NEWS FEED FUNCTIONS
# ===================================================
def fetch_news():
    """Fetch latest headlines from FinancialJuice RSS feed"""
    global news_cache, news_last_poll
    try:
        feed = feedparser.parse(FJ_RSS_URL)
        headlines = []
        for entry in feed.entries[:FJ_MAX_HEADLINES]:
            published = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6]).isoformat()
            headlines.append({
                "title": entry.get("title", "").strip(),
                "published": published,
                "link": entry.get("link", ""),
            })
        with news_cache_lock:
            news_cache = headlines
            news_last_poll = time.time()
        print(f"NEWS: Fetched {len(headlines)} headlines from FinancialJuice")
    except Exception as e:
        print(f"NEWS: Error fetching feed: {e}")

def news_poll_loop():
    """Background thread: poll FinancialJuice RSS every FJ_POLL_INTERVAL seconds"""
    while True:
        try:
            fetch_news()
        except Exception as e:
            print(f"NEWS: Poll loop error: {e}")
        time.sleep(FJ_POLL_INTERVAL)

def start_news_thread():
    """Start the background news polling thread (daemon so it dies with the app)"""
    if not ENABLE_NEWS:
        print("NEWS: Disabled via ENABLE_NEWS=false")
        return
    t = threading.Thread(target=news_poll_loop, daemon=True, name="news-poller")
    t.start()
    print(f"NEWS: Background poller started (interval={FJ_POLL_INTERVAL}s)")

def get_news_context(max_items=5):
    """Build a news context string for Claude from cached headlines"""
    with news_cache_lock:
        items = list(news_cache)
    if not items:
        return ""
    recent = items[:max_items]
    lines = ["\nRECENT MARKET NEWS (FinancialJuice):"]
    for item in recent:
        ts = ""
        if item["published"]:
            try:
                dt = datetime.fromisoformat(item["published"])
                ts = dt.strftime("%H:%M CT")
            except Exception:
                ts = item["published"][:16]
        lines.append(f"  [{ts}] {item['title']}")
    lines.append("  (Use news for context only — do not override signal logic based on headlines)")
    return "\n".join(lines)

# ===================================================
# ONEMP11 V8.1b KNOWLEDGE BASE (updated)
# ===================================================
SYSTEM_KNOWLEDGE = """
You are the OneMP11 V8.1b trading system analyst for ES futures.
You have deep knowledge of this specific system's signal logic:

SIGNAL GENERATION:
- Entries require ALL four: CVD Momentum > 60 (Strong Threshold),
  Histogram > 5, Price above/below Kalman VWAP (TL), Kalman slope confirms direction
- ADX must be > 20 (trending market) for entries and re-entries only
- Reversals fire when ALL four conditions flip — ADX does NOT gate reversals
- Re-entries fire on momentum flip back to trend direction (max 3 per trend)
- No lastTradeWin gating — re-entries allowed after any exit

KALMAN VWAP (referred to as "TL" / Trend Line in alerts):
- Higher-order Kalman filter smoothing price + VWAP blend
- Process Noise: 0.08 (responsive), Measurement Noise: 6
- Slope > 0.1 = bullish, < -0.1 = bearish (min slope strength filter)
- Price must be on correct side of TL for entry

TL SPREAD CLASSIFICATION (ATR-based, self-adjusting to volatility):
- TIGHT (< 0.5× ATR): Price hugging trend line — strong conviction zone
- RIDING (0.5-1.0× ATR): Normal trend following distance
- EXTENDED (1.0-2.0× ATR): Getting stretched — trail tight
- STRETCHED (> 2.0× ATR): Overextended — high reversion risk

CVD FLOW (Cumulative Volume Delta):
- Uses Kalman smoothing (adaptive, not fixed EMA)
- CVD Kalman Process Noise: 0.08
- Momentum: normalized ROC of CVD, range -100 to +100
- Histogram (Vol Intensity): volume-weighted delta strength, range -100 to +100
- Uses 1-min LTF delta during RTH, bar-range approximation during overnight

RSI CONTEXT (display only, NOT used in entry signals):
- ES RSI: Main instrument relative strength (14-period)
- VIX RSI: Fear gauge momentum (only valid during RTH 8am-3pm CT, flat overnight)
- Compare RSI (default CL/crude): Cross-market confirmation (configurable symbol)
- RSI spread (ES RSI - VIX RSI): Positive = risk-on, negative = risk-off

SESSION FILTER:
- No-entry zone: 2pm-8pm CT (blocks fresh entries and re-entries, NOT reversals)
- Force close: 4pm CT Mon-Thu (always ON — protects winning trades)
- Friday auto-close: Always force-close at 4pm Friday (market closed Fri 4pm - Sun 5pm)
- Reversals blocked during no-entry zone (Power Hour trades have 23% WR historically)
- Session open alert fires at 8pm with prev day OHLC + 5pm reopen price

VIX REGIME (with hysteresis ±1 buffer to prevent flip-flop):
- LOW: VIX < 14 (must drop below 14 from NORMAL)
- NORMAL: VIX 15-25
- HIGH: VIX > 26 (must cross 26 from NORMAL, drop below 24 to go back)
- EXTREME: VIX > 36

REGIME TUNING GUIDE:
  Setting              | High VIX(25+) | Normal(15-25) | Low(<15)
  Strong Threshold     | 60            | 55            | 50
  Kalman VWAP PN       | 0.08          | 0.05          | 0.03
  CVD Kalman PN        | 0.08          | 0.05          | 0.03
  Min Slope Strength   | 0.1           | 0.1           | 0.05
  No-Entry Start       | 14 (2pm)      | 15 (3pm)      | 16 (4pm)
  ADX Threshold        | 20            | 20            | 18
  Histogram Min        | 5             | 5             | 3

P&L TRACKING:
- Daily: resets at midnight CT via timeframe.change("D")
- Weekly: resets on Monday via timeframe.change("W")
- Monthly: resets on 1st via timeframe.change("M")
- Total: tracks from chart start or configurable start date
- Session stats: OPEN(8-10), MIDDAY(10-12), AFTRN(12-2), POWER(2-4), O/N(rest)

HISTORICAL PERFORMANCE (Feb-Apr 2026, high VIX regime):
- O/N: 75% WR, +$35/trade — BEST session, carries the system
- OPEN: 60% WR, +$7/trade — solid second
- MIDDAY: 49% WR, -$1.8/trade — known drag, not worth blocking (hurts O/N)
- POWER: blocked (23% WR when allowed)
- Overall: ~54% WR, 1.23 R:R, avg win +31pts, avg loss -25pts

ALERT TYPES:
- ENTRY: Fresh long/short — all 4 conditions aligned (strongest signal)
- RE_ENTRY: Momentum flipped back to trend after pullback (#1, #2, #3)
- REVERSAL: All conditions flipped — exits current AND enters opposite
- SESSION_CLOSE: Force exit at 4pm CT (Mon-Thu)
- FRIDAY_CLOSE: Force exit at 4pm Friday (market closed weekend)
- SL: Stop loss hit (dormant by default — SL is OFF)
- TREND_OVER: Price crossed TL while flat — no longer watching
- MILESTONE_UP: Trade hit +10, +20, or +30 pts (fires immediately)
- MILESTONE_DOWN: Trade hit -15 (warning) or -25 (danger) pts (fires immediately)
- MARKET_CHECK: 10am CST daily snapshot before midday chop
- REGIME_SHIFT: VIX regime changed with hysteresis buffer
- NO_ENTRY: 2pm power hour block started
- SESSION_OPEN: 8pm session open with prev day levels + 5pm gap

VERDICT LOGIC (on milestone alerts):
- Combines ADX trend strength, momentum direction, TL spread, RSI
- UP verdicts: "💪 Trend strong with room — hold" / "⚡ Strong but extended — trail tight" /
  "📈 Strong but RSI hot — protect gains" / "🚨 Stretched — consider partial TP"
- DOWN verdicts: "🔄 Normal pullback in strong trend — hold" /
  "⚠️ Dip but trend intact — watch closely" / "🚨 Trend weakening — consider cutting"

CONFIDENCE ASSESSMENT RULES:
- HIGH: Entry/reversal with ADX>25, strong momentum, TL TIGHT/RIDING, RSI 40-60
- MEDIUM: Conditions mostly aligned but one concern (extended TL, fading mom, session risk)
- LOW: Multiple concerns (low ADX, STRETCHED TL, RSI extreme, midday session, against VIX)
- For RE_ENTRY: slightly lower confidence than fresh ENTRY (trend already partially played)
- Session context: OPEN and O/N entries deserve higher confidence than MIDDAY
- Streak context: 3+ losses in a row = lower confidence regardless of conditions
"""

# ===================================================
# DATABASE
# ===================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            alert_type TEXT,
            direction TEXT,
            price REAL,
            exit_pts REAL,
            daily_pnl REAL,
            weekly_pnl REAL,
            monthly_pnl REAL,
            total_pnl REAL,
            tl_spread REAL,
            tl_state TEXT,
            rsi REAL,
            vix_rsi REAL,
            compare_rsi REAL,
            adx REAL,
            verdict TEXT,
            claude_analysis TEXT,
            claude_confidence TEXT,
            raw_json TEXT,
            session TEXT
        )
    """)
    # Add session column if upgrading from V2.0
    try:
        c.execute("ALTER TABLE alerts ADD COLUMN session TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    conn.close()

init_db()


def get_session_from_time(ts_str):
    """Determine session window from timestamp"""
    try:
        dt = datetime.fromisoformat(ts_str)
        h = dt.hour
    except Exception:
        h = 12
    if 8 <= h < 10:
        return "OPEN"
    elif 10 <= h < 12:
        return "MIDDAY"
    elif 12 <= h < 14:
        return "AFTERNOON"
    elif 14 <= h < 16:
        return "POWER"
    else:
        return "OVERNIGHT"


def store_alert(data):
    session = get_session_from_time(data.get("timestamp", ""))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO alerts (
            timestamp, alert_type, direction, price, exit_pts,
            daily_pnl, weekly_pnl, monthly_pnl, total_pnl,
            tl_spread, tl_state, rsi, vix_rsi, compare_rsi,
            adx, verdict, claude_analysis, claude_confidence, raw_json, session
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("timestamp", ""),
        data.get("alert_type", ""),
        data.get("direction", ""),
        data.get("price", 0),
        data.get("exit_pts", 0),
        data.get("daily_pnl", 0),
        data.get("weekly_pnl", 0),
        data.get("monthly_pnl", 0),
        data.get("total_pnl", 0),
        data.get("tl_spread", 0),
        data.get("tl_state", ""),
        data.get("rsi", 0),
        data.get("vix_rsi", 0),
        data.get("compare_rsi", 0),
        data.get("adx", 0),
        data.get("verdict", ""),
        data.get("claude_analysis", ""),
        data.get("claude_confidence", ""),
        data.get("raw_json", ""),
        session
    ))
    conn.commit()
    conn.close()


def get_recent_alerts(n=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (n,))
    rows = c.fetchall()
    cols = [desc[0] for desc in c.description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def get_stats(days=7):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    c.execute("""
        SELECT alert_type, direction, exit_pts, claude_confidence, session
        FROM alerts WHERE timestamp > ? AND exit_pts != 0
        ORDER BY id DESC
    """, (cutoff,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"trades": 0, "wins": 0, "losses": 0, "total_pts": 0, "avg_pts": 0, "streak": "", "win_rate": 0}

    wins = sum(1 for r in rows if r[2] > 0)
    losses = sum(1 for r in rows if r[2] < 0)
    total_pts = sum(r[2] for r in rows)
    win_pts = [r[2] for r in rows if r[2] > 0]
    loss_pts = [r[2] for r in rows if r[2] < 0]

    streak = 0
    streak_dir = ""
    for r in rows:
        if r[2] > 0:
            if streak_dir in ("", "W"):
                streak += 1
                streak_dir = "W"
            else:
                break
        elif r[2] < 0:
            if streak_dir in ("", "L"):
                streak += 1
                streak_dir = "L"
            else:
                break

    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "total_pts": round(total_pts, 2),
        "avg_pts": round(total_pts / len(rows), 2) if rows else 0,
        "avg_win": round(sum(win_pts) / len(win_pts), 2) if win_pts else 0,
        "avg_loss": round(sum(loss_pts) / len(loss_pts), 2) if loss_pts else 0,
        "win_rate": round(wins / len(rows) * 100, 1) if rows else 0,
        "streak": f"{streak}{streak_dir}"
    }


def get_session_stats():
    """Get performance breakdown by session window"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, alert_type, direction, exit_pts, claude_confidence, session
        FROM alerts WHERE exit_pts != 0
        ORDER BY id DESC LIMIT 200
    """)
    rows = c.fetchall()
    conn.close()

    sessions = {}
    for name in ["OPEN", "MIDDAY", "AFTERNOON", "POWER", "OVERNIGHT"]:
        sessions[name] = {"trades": 0, "wins": 0, "pts": 0}

    for row in rows:
        session = row[5] if row[5] else get_session_from_time(row[0])
        pts = row[3]

        if session not in sessions:
            sessions[session] = {"trades": 0, "wins": 0, "pts": 0}

        sessions[session]["trades"] += 1
        sessions[session]["pts"] += pts
        if pts > 0:
            sessions[session]["wins"] += 1

    for k, v in sessions.items():
        v["win_rate"] = round(v["wins"] / v["trades"] * 100, 1) if v["trades"] > 0 else 0
        v["avg_pts"] = round(v["pts"] / v["trades"], 2) if v["trades"] > 0 else 0
        v["pts"] = round(v["pts"], 2)

    return sessions


def get_similar_trades(alert_data, limit=30):
    """Find similar past trades for pattern matching"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    direction = alert_data.get("direction", "")
    tl_state = alert_data.get("tl_state", "")
    session = get_session_from_time(alert_data.get("timestamp", ""))

    # Get trades with same direction
    c.execute("""
        SELECT alert_type, direction, exit_pts, tl_state, rsi, adx,
               claude_confidence, session
        FROM alerts
        WHERE direction = ? AND exit_pts != 0
        ORDER BY id DESC LIMIT ?
    """, (direction, limit))
    all_dir = c.fetchall()

    # Get trades with same TL state
    c.execute("""
        SELECT exit_pts FROM alerts
        WHERE tl_state = ? AND exit_pts != 0
        ORDER BY id DESC LIMIT ?
    """, (tl_state, limit))
    tl_trades = c.fetchall()

    # Get trades in same session
    c.execute("""
        SELECT exit_pts FROM alerts
        WHERE session = ? AND exit_pts != 0
        ORDER BY id DESC LIMIT ?
    """, (session, limit))
    sess_trades = c.fetchall()

    conn.close()

    result = {
        "direction_trades": len(all_dir),
        "direction_wins": sum(1 for t in all_dir if t[2] > 0),
        "direction_wr": 0,
        "tl_state": tl_state,
        "tl_trades": len(tl_trades),
        "tl_wins": sum(1 for t in tl_trades if t[0] > 0),
        "tl_wr": 0,
        "session": session,
        "session_trades": len(sess_trades),
        "session_wins": sum(1 for t in sess_trades if t[0] > 0),
        "session_wr": 0,
    }

    if result["direction_trades"] > 0:
        result["direction_wr"] = round(result["direction_wins"] / result["direction_trades"] * 100, 1)
    if result["tl_trades"] > 0:
        result["tl_wr"] = round(result["tl_wins"] / result["tl_trades"] * 100, 1)
    if result["session_trades"] > 0:
        result["session_wr"] = round(result["session_wins"] / result["session_trades"] * 100, 1)

    return result


def get_pattern_analysis():
    """Analyze patterns in alert history for Claude context"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT alert_type, direction, exit_pts, tl_state, rsi, adx, claude_confidence, session
        FROM alerts WHERE exit_pts != 0
        ORDER BY id DESC LIMIT 50
    """)
    trades = c.fetchall()
    conn.close()

    if not trades:
        return "No trade history yet."

    long_trades = [t for t in trades if t[1] == "LONG"]
    short_trades = [t for t in trades if t[1] == "SHORT"]
    long_wins = sum(1 for t in long_trades if t[2] > 0)
    short_wins = sum(1 for t in short_trades if t[2] > 0)

    tight_trades = [t for t in trades if t[3] == "TIGHT"]
    riding_trades = [t for t in trades if t[3] == "RIDING"]
    extended_trades = [t for t in trades if t[3] == "EXTENDED"]
    stretched_trades = [t for t in trades if t[3] == "STRETCHED"]

    high_adx = [t for t in trades if t[5] and t[5] >= 25]
    low_adx = [t for t in trades if t[5] and t[5] < 25]

    # Session breakdown
    session_map = {}
    for t in trades:
        s = t[7] if t[7] else "UNKNOWN"
        if s not in session_map:
            session_map[s] = {"trades": 0, "wins": 0}
        session_map[s]["trades"] += 1
        if t[2] > 0:
            session_map[s]["wins"] += 1

    # Entry vs re-entry
    entries = [t for t in trades if t[0] == "ENTRY"]
    re_entries = [t for t in trades if t[0] == "RE_ENTRY"]
    reversals = [t for t in trades if t[0] == "REVERSAL"]

    streak = 0
    streak_dir = ""
    for t in trades:
        if t[2] > 0:
            if streak_dir in ("", "W"):
                streak += 1
                streak_dir = "W"
            else:
                break
        else:
            if streak_dir in ("", "L"):
                streak += 1
                streak_dir = "L"
            else:
                break

    high_conf = [t for t in trades if t[6] == "HIGH"]
    high_conf_wins = sum(1 for t in high_conf if t[2] > 0)
    med_conf = [t for t in trades if t[6] == "MEDIUM"]
    med_conf_wins = sum(1 for t in med_conf if t[2] > 0)

    lines = []
    total_wins = sum(1 for t in trades if t[2] > 0)
    total_losses = sum(1 for t in trades if t[2] < 0)
    lines.append(f"TRADE HISTORY ({len(trades)} recent trades):")
    lines.append(f"  Overall: {total_wins}W / {total_losses}L ({round(total_wins/len(trades)*100) if trades else 0}%)")
    lines.append(f"  Current streak: {streak}{streak_dir}")

    if long_trades:
        pct = round(long_wins / len(long_trades) * 100)
        lines.append(f"  LONG: {long_wins}/{len(long_trades)} wins ({pct}%)")
    if short_trades:
        pct = round(short_wins / len(short_trades) * 100)
        lines.append(f"  SHORT: {short_wins}/{len(short_trades)} wins ({pct}%)")

    lines.append(f"  BY TL STATE:")
    for label, group in [("TIGHT", tight_trades), ("RIDING", riding_trades),
                          ("EXTENDED", extended_trades), ("STRETCHED", stretched_trades)]:
        if group:
            w = sum(1 for t in group if t[2] > 0)
            lines.append(f"    {label}: {w}/{len(group)} wins ({round(w/len(group)*100)}%)")

    if high_adx:
        haw = sum(1 for t in high_adx if t[2] > 0)
        lines.append(f"  High ADX (25+): {haw}/{len(high_adx)} wins ({round(haw/len(high_adx)*100)}%)")
    if low_adx:
        law = sum(1 for t in low_adx if t[2] > 0)
        lines.append(f"  Low ADX (<25): {law}/{len(low_adx)} wins ({round(law/len(low_adx)*100)}%)")

    lines.append(f"  BY SESSION:")
    for s, d in session_map.items():
        wr = round(d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        lines.append(f"    {s}: {d['wins']}/{d['trades']} wins ({wr}%)")

    if entries:
        ew = sum(1 for t in entries if t[2] > 0)
        lines.append(f"  ENTRY: {ew}/{len(entries)} wins ({round(ew/len(entries)*100)}%)")
    if re_entries:
        rw = sum(1 for t in re_entries if t[2] > 0)
        lines.append(f"  RE_ENTRY: {rw}/{len(re_entries)} wins ({round(rw/len(re_entries)*100)}%)")
    if reversals:
        rv = sum(1 for t in reversals if t[2] > 0)
        lines.append(f"  REVERSAL: {rv}/{len(reversals)} wins ({round(rv/len(reversals)*100)}%)")

    lines.append(f"  CLAUDE ACCURACY:")
    if high_conf:
        lines.append(f"    HIGH conf: {high_conf_wins}/{len(high_conf)} wins ({round(high_conf_wins/len(high_conf)*100)}%)")
    if med_conf:
        lines.append(f"    MEDIUM conf: {med_conf_wins}/{len(med_conf)} wins ({round(med_conf_wins/len(med_conf)*100)}%)")

    return "\n".join(lines)


def delete_alert(alert_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted > 0


def clear_all_alerts():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM alerts")
    count = c.rowcount
    conn.commit()
    conn.close()
    return count


# ===================================================
# ALERT PARSER (synced with V8.1b alert format)
# ===================================================
def parse_alert(raw_json):
    """Parse the TradingView Discord JSON into structured data"""
    data = {
        "alert_type": "",
        "direction": "",
        "price": 0,
        "exit_pts": 0,
        "daily_pnl": 0,
        "weekly_pnl": 0,
        "monthly_pnl": 0,
        "total_pnl": 0,
        "tl_spread": 0,
        "tl_state": "",
        "rsi": 0,
        "vix_rsi": 0,
        "compare_rsi": 0,
        "adx": 0,
        "verdict": "",
        "raw_json": json.dumps(raw_json) if isinstance(raw_json, dict) else str(raw_json),
        "timestamp": datetime.utcnow().isoformat()
    }

    try:
        content = raw_json.get("content", "") if isinstance(raw_json, dict) else ""

        # === ALERT TYPE DETECTION (order matters) ===

        # Re-entries (check before generic ENTRY)
        if "RE-LONG" in content:
            data["alert_type"] = "RE_ENTRY"
            data["direction"] = "LONG"
        elif "RE-SHORT" in content:
            data["alert_type"] = "RE_ENTRY"
            data["direction"] = "SHORT"
        # Fresh entries
        elif "GO LONG" in content and "REVERSAL" not in content:
            data["alert_type"] = "ENTRY"
            data["direction"] = "LONG"
        elif "GO SHORT" in content and "REVERSAL" not in content:
            data["alert_type"] = "ENTRY"
            data["direction"] = "SHORT"
        # Reversal (exit + new entry)
        elif "REVERSAL" in content:
            data["alert_type"] = "REVERSAL"
            # Direction = the NEW trade direction
            if "GO LONG" in content:
                data["direction"] = "LONG"
            elif "GO SHORT" in content:
                data["direction"] = "SHORT"
            else:
                data["direction"] = "LONG" if "Exited SHORT" in content else "SHORT"
        # Exits
        elif "FRIDAY CLOSE" in content:
            data["alert_type"] = "FRIDAY_CLOSE"
            data["direction"] = "LONG" if "Exited LONG" in content else "SHORT"
        elif "4PM CLOSE" in content:
            data["alert_type"] = "SESSION_CLOSE"
            data["direction"] = "LONG" if "Exited LONG" in content else "SHORT"
        elif "SL —" in content or "SL LONG" in content or "SL SHORT" in content:
            data["alert_type"] = "SL"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        # Milestones
        elif " up " in content and "pts" in content and ("LONG" in content or "SHORT" in content):
            data["alert_type"] = "MILESTONE_UP"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif " down " in content and "pts" in content and ("LONG" in content or "SHORT" in content):
            data["alert_type"] = "MILESTONE_DOWN"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        # Info alerts
        elif "TREND OVER" in content:
            data["alert_type"] = "TREND_OVER"
        elif "10am MARKET CHECK" in content or "MARKET CHECK" in content:
            data["alert_type"] = "MARKET_CHECK"
        elif "REGIME SHIFT" in content:
            data["alert_type"] = "REGIME_SHIFT"
        elif "NO-ENTRY ZONE" in content:
            data["alert_type"] = "NO_ENTRY"
        elif "SESSION OPEN" in content:
            data["alert_type"] = "SESSION_OPEN"

        # === FIELD EXTRACTION ===

        # Price (first $ amount)
        price_match = re.search(r'\$(\d[\d,.]*)', content)
        if price_match:
            data["price"] = float(price_match.group(1).replace(",", ""))

        # Exit points — match "Exited LONG +20.88pts" or "+20.88pts ("
        exit_match = re.search(r'(?:Exited \w+\s*)?([+-]?\d+\.?\d*)pts\s*\(', content)
        if exit_match:
            data["exit_pts"] = float(exit_match.group(1))

        # Daily/Weekly/Monthly P&L — "Today: +20.9 │ Week: +243.9 │ Month: +130.1"
        today_match = re.search(r'Today:\s*([+-]?\d+\.?\d*)', content)
        if today_match:
            data["daily_pnl"] = float(today_match.group(1))

        week_match = re.search(r'Week:\s*([+-]?\d+\.?\d*)', content)
        if week_match:
            data["weekly_pnl"] = float(week_match.group(1))

        month_match = re.search(r'Month:\s*([+-]?\d+\.?\d*)', content)
        if month_match:
            data["monthly_pnl"] = float(month_match.group(1))

        # Total P&L — "Total: +2,420.8pts"
        total_match = re.search(r'Total:\s*([+-]?\d[\d,.]*?)pts', content)
        if total_match:
            data["total_pnl"] = float(total_match.group(1).replace(",", ""))

        # TL Spread — "TL: +8.5pts RIDING"
        tl_match = re.search(r'TL:\s*([+-]?\d+\.?\d*)pts\s*(\w+)', content)
        if tl_match:
            data["tl_spread"] = float(tl_match.group(1))
            data["tl_state"] = tl_match.group(2).upper()

        # RSI — "RSI: 62"
        rsi_match = re.search(r'RSI:\s*(\d+)', content)
        if rsi_match:
            data["rsi"] = float(rsi_match.group(1))

        # VIX RSI — "VIX: 38" (not "VIX: off hrs")
        vix_match = re.search(r'VIX:\s*(\d+)', content)
        if vix_match:
            data["vix_rsi"] = float(vix_match.group(1))

        # Compare RSI — "CL: 55" (configurable label)
        cl_match = re.search(r'(?:CL|GC|NQ|DX):\s*(\d+)', content)
        if cl_match:
            data["compare_rsi"] = float(cl_match.group(1))

        # ADX — "ADX: 35"
        adx_match = re.search(r'ADX:\s*(\d+)', content)
        if adx_match:
            data["adx"] = float(adx_match.group(1))

        # Verdict line from milestones — "💪 Trend strong..." or "🔄 Normal pullback..."
        verdict_patterns = [
            r'(💪[^\n]+)',
            r'(⚡[^\n]+(?:trail|extended)[^\n]*)',
            r'(📈[^\n]+(?:RSI|protect)[^\n]*)',
            r'(🚨[^\n]+(?:Stretched|cutting|weakening)[^\n]*)',
            r'(⚠️[^\n]+(?:fading|watch|tighten)[^\n]*)',
            r'(🔄[^\n]+(?:pullback|hold)[^\n]*)',
            r'(📊[^\n]+(?:Moderate|awareness)[^\n]*)',
        ]
        for vp in verdict_patterns:
            vm = re.search(vp, content)
            if vm:
                data["verdict"] = vm.group(1).strip()
                break

    except Exception as e:
        data["alert_type"] = "PARSE_ERROR"
        data["verdict"] = str(e)

    return data


# ===================================================
# CLAUDE ANALYSIS
# ===================================================
def analyze_with_claude(alert_data, recent_alerts):
    """Send alert + database history context + news to Claude for analysis"""
    if not ANTHROPIC_API_KEY or not ENABLE_CLAUDE:
        return "", ""

    pattern_context = get_pattern_analysis()
    similar = get_similar_trades(alert_data)

    # Get live news context from FinancialJuice
    news_context = get_news_context(max_items=5)

    recent_context = ""
    if recent_alerts:
        recent_context = "\nRecent alerts (newest first):\n"
        for a in recent_alerts[:10]:
            conf_tag = f" [Claude: {a.get('claude_confidence', '')}]" if a.get('claude_confidence') else ""
            pts_tag = f" exit:{a.get('exit_pts', 0):+.1f}pts" if a.get('exit_pts', 0) != 0 else ""
            sess_tag = f" ({a.get('session', '')})" if a.get('session') else ""
            recent_context += f"  {a.get('alert_type', '')} {a.get('direction', '')} @ {a.get('price', 0)}{pts_tag}{sess_tag}{conf_tag}\n"

    similar_context = ""
    if similar["direction_trades"] > 0:
        similar_context = f"""
SIMILAR TRADE PATTERNS:
  {similar['direction_trades']} {alert_data.get('direction', '')} trades: {similar['direction_wr']}% win rate
  {similar['tl_trades']} {similar['tl_state']} entries: {similar['tl_wr']}% win rate
  {similar['session_trades']} {similar['session']} session trades: {similar['session_wr']}% win rate"""

    prompt = f"""{SYSTEM_KNOWLEDGE}

DATABASE CONTEXT (your trade history):
{pattern_context}
{similar_context}
{recent_context}
{news_context}

CURRENT ALERT TO ANALYZE:
  Type: {alert_data.get('alert_type', '')}
  Direction: {alert_data.get('direction', '')}
  Price: {alert_data.get('price', 0)}
  RSI: {alert_data.get('rsi', 0)}
  ADX: {alert_data.get('adx', 0)}
  VIX RSI: {alert_data.get('vix_rsi', 0)} {"(off hours)" if alert_data.get('vix_rsi', 0) == 0 else ""}
  CL RSI: {alert_data.get('compare_rsi', 0)}
  TL Spread: {alert_data.get('tl_spread', 0)} ({alert_data.get('tl_state', '')})
  TV Verdict: {alert_data.get('verdict', 'none')}
  Session: {get_session_from_time(alert_data.get('timestamp', ''))}

Provide a brief technical assessment (2-3 sentences max). Be specific — reference
database patterns (e.g. "Your LONG entries from RIDING state have won 68% of the time").
Mention the session context if relevant. If recent news headlines are provided and
clearly relevant (e.g. FOMC, CPI, NFP, tariffs, major geopolitical events), briefly
note the potential impact — but do NOT override signal logic based on news alone.
End with exactly one of: [HIGH CONFIDENCE], [MEDIUM CONFIDENCE], or [LOW CONFIDENCE]."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 250,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            text = result["content"][0]["text"]

            confidence = "MEDIUM"
            if "[HIGH CONFIDENCE]" in text:
                confidence = "HIGH"
            elif "[LOW CONFIDENCE]" in text:
                confidence = "LOW"

            clean = text.replace("[HIGH CONFIDENCE]", "").replace("[MEDIUM CONFIDENCE]", "").replace("[LOW CONFIDENCE]", "").strip()
            return clean, confidence

    except Exception as e:
        print(f"Claude API error: {e}")

    return "", ""


# ===================================================
# DISCORD RICH EMBEDS
# ===================================================
ALERT_STYLES = {
    "ENTRY":          {"emoji": "🟢", "color": 3066993,  "label": "ENTRY SIGNAL"},
    "RE_ENTRY":       {"emoji": "🔁", "color": 3447003,  "label": "RE-ENTRY"},
    "SESSION_CLOSE":  {"emoji": "⏸", "color": 10070709, "label": "4PM CLOSE"},
    "FRIDAY_CLOSE":   {"emoji": "🔒", "color": 10070709, "label": "FRIDAY CLOSE"},
    "REVERSAL":       {"emoji": "🔄", "color": 15844367, "label": "REVERSAL"},
    "SL":             {"emoji": "🛑", "color": 15158332, "label": "STOP LOSS"},
    "TREND_OVER":     {"emoji": "❌", "color": 10038562, "label": "TREND OVER"},
    "MILESTONE_UP":   {"emoji": "📈", "color": 3066993,  "label": "MILESTONE UP"},
    "MILESTONE_DOWN": {"emoji": "📉", "color": 15158332, "label": "MILESTONE DOWN"},
    "MARKET_CHECK":   {"emoji": "📋", "color": 3447003,  "label": "MARKET CHECK"},
    "REGIME_SHIFT":   {"emoji": "🌡️", "color": 15844367, "label": "REGIME SHIFT"},
    "NO_ENTRY":       {"emoji": "⏸", "color": 10038562, "label": "NO-ENTRY ZONE"},
    "SESSION_OPEN":   {"emoji": "🔔", "color": 3447003,  "label": "SESSION OPEN"},
    "PARSE_ERROR":    {"emoji": "❓", "color": 9807270,  "label": "UNKNOWN ALERT"},
}


def build_discord_embed(alert_data, claude_analysis="", claude_confidence="", is_test=False):
    """Build a rich Discord embed for the alert"""
    atype = alert_data.get("alert_type", "")
    style = ALERT_STYLES.get(atype, ALERT_STYLES["PARSE_ERROR"])

    direction = alert_data.get("direction", "")
    if direction == "LONG":
        dir_emoji = "🟢"
        dir_label = "LONG ↑"
    elif direction == "SHORT":
        dir_emoji = "🔴"
        dir_label = "SHORT ↓"
    else:
        dir_emoji = "⚪"
        dir_label = "—"

    # Color by direction for entry types
    if atype in ("ENTRY", "RE_ENTRY") and direction == "SHORT":
        style = {**style, "color": 15158332}

    title = f"{style['emoji']} {style['label']}"
    if is_test:
        title = f"🧪 TEST — {title}"

    price = alert_data.get("price", 0)
    price_str = f"${price:,.2f}" if price else "—"
    desc_lines = []
    if direction:
        desc_lines.append(f"## {dir_emoji} {dir_label} │ ES @ {price_str}")
    elif price:
        desc_lines.append(f"## ES @ {price_str}")

    verdict = alert_data.get("verdict", "")
    if verdict:
        desc_lines.append(f"\n> {verdict}")

    description = "\n".join(desc_lines)
    fields = []

    # Technicals
    rsi = alert_data.get("rsi", 0)
    adx = alert_data.get("adx", 0)
    vix = alert_data.get("vix_rsi", 0)
    cl = alert_data.get("compare_rsi", 0)

    if rsi or adx or vix or cl:
        fields.append({"name": "\u200b", "value": "**📊 Technicals**", "inline": False})
        if rsi:
            rsi_bar = "🟢" if 40 <= rsi <= 60 else "🟡" if 30 <= rsi <= 70 else "🔴"
            fields.append({"name": "RSI", "value": f"{rsi_bar} **{rsi:.0f}**", "inline": True})
        if adx:
            adx_bar = "💪" if adx >= 25 else "💤"
            fields.append({"name": "ADX", "value": f"{adx_bar} **{adx:.0f}**", "inline": True})
        if rsi or adx:
            fields.append({"name": "\u200b", "value": "\u200b", "inline": True})
        if vix:
            vix_emoji = "🔴" if vix > 60 else "🟡" if vix > 40 else "🟢"
            fields.append({"name": "VIX RSI", "value": f"{vix_emoji} **{vix:.0f}**", "inline": True})
        if cl:
            fields.append({"name": "CL RSI", "value": f"**{cl:.0f}**", "inline": True})
        if vix or cl:
            fields.append({"name": "\u200b", "value": "\u200b", "inline": True})

    # TL Spread
    tl_spread = alert_data.get("tl_spread", 0)
    tl_state = alert_data.get("tl_state", "")
    if tl_spread or tl_state:
        tl_emoji = "📈" if tl_spread >= 0 else "📉"
        state_emoji = "🎯" if tl_state == "TIGHT" else "🏄" if tl_state == "RIDING" else "⚡" if tl_state == "EXTENDED" else "🚨" if tl_state == "STRETCHED" else ""
        fields.append({"name": f"{tl_emoji} TL Spread", "value": f"**{tl_spread:+.1f} pts** — {state_emoji} {tl_state}", "inline": False})

    # P&L
    daily = alert_data.get("daily_pnl", 0)
    weekly = alert_data.get("weekly_pnl", 0)
    monthly = alert_data.get("monthly_pnl", 0)
    total = alert_data.get("total_pnl", 0)
    exit_pts = alert_data.get("exit_pts", 0)

    if daily or weekly or monthly or total or exit_pts:
        fields.append({"name": "\u200b", "value": "**💰 P&L**", "inline": False})
        if exit_pts:
            e_emoji = "✅" if exit_pts > 0 else "❌"
            fields.append({"name": "Trade", "value": f"{e_emoji} **{exit_pts:+.1f} pts**", "inline": True})
        if daily:
            d_emoji = "✅" if daily >= 0 else "❌"
            fields.append({"name": "Today", "value": f"{d_emoji} **{daily:+.1f}**", "inline": True})
        if weekly:
            w_emoji = "✅" if weekly >= 0 else "❌"
            fields.append({"name": "Week", "value": f"{w_emoji} **{weekly:+.1f}**", "inline": True})
        if monthly:
            m_emoji = "✅" if monthly >= 0 else "❌"
            fields.append({"name": "Month", "value": f"{m_emoji} **{monthly:+.1f}**", "inline": True})
        if total:
            t_emoji = "🏆" if total >= 0 else "📉"
            fields.append({"name": "Total", "value": f"{t_emoji} **{total:+.1f} pts**", "inline": True})

    embed = {
        "title": title,
        "description": description,
        "color": style["color"],
        "fields": fields,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "footer": {"text": "⚡ © 2026 OneMP11 V8.1b"}
    }
    embeds = [embed]

    if claude_analysis:
        if claude_confidence == "HIGH":
            c_color, c_emoji, c_label = 3066993, "🟢", "HIGH CONFIDENCE"
        elif claude_confidence == "MEDIUM":
            c_color, c_emoji, c_label = 15844367, "🟡", "MEDIUM CONFIDENCE"
        else:
            c_color, c_emoji, c_label = 15158332, "🔴", "LOW CONFIDENCE"

        claude_embed = {
            "author": {"name": "🤖 Claude Analysis"},
            "description": f"{c_emoji} **{c_label}**\n{'─' * 20}\n{claude_analysis}",
            "color": c_color,
        }
        if is_test:
            claude_embed["footer"] = {"text": "🧪 TEST — not stored in database"}
        embeds.append(claude_embed)
    elif is_test:
        embed["footer"] = {"text": "🧪 TEST — not stored in database"}

    return embeds


def forward_to_discord(alert_data, claude_analysis="", claude_confidence="", is_test=False):
    """Forward alert to Discord as rich embed"""
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD: No webhook URL configured")
        return False

    try:
        embeds = build_discord_embed(alert_data, claude_analysis, claude_confidence, is_test)
        payload = {"embeds": embeds, "username": "OneMP11"}

        print(f"DISCORD: Sending {len(embeds)} embed(s)...")
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"DISCORD: Status={response.status_code}")
        if response.status_code not in [200, 204]:
            print(f"DISCORD: Error: {response.text[:500]}")
            return False
        return True

    except Exception as e:
        print(f"DISCORD: Exception: {e}")
        traceback.print_exc()
        return False


# ===================================================
# ROUTES
# ===================================================
@app.route("/", methods=["GET"])
def health():
    stats = get_stats(7)
    return jsonify({
        "status": "running",
        "service": "OneMP11 Alert Server",
        "version": "2.1 (Synced V8.1b)",
        "claude_enabled": ENABLE_CLAUDE,
        "discord_configured": bool(DISCORD_WEBHOOK_URL),
        "last_7_days": stats
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """Receive TradingView webhook, analyze with Claude + DB history, forward to Discord"""
    try:
        raw = request.get_json(force=True)
    except Exception:
        try:
            raw = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400

    alert_data = parse_alert(raw)
    recent = get_recent_alerts(10)

    claude_analysis = ""
    claude_confidence = ""
    # Skip Claude for info-only alerts
    skip_types = ["NO_ENTRY", "TREND_OVER", "REGIME_SHIFT", "SESSION_OPEN"]
    if alert_data["alert_type"] not in skip_types:
        claude_analysis, claude_confidence = analyze_with_claude(alert_data, recent)

    alert_data["claude_analysis"] = claude_analysis
    alert_data["claude_confidence"] = claude_confidence

    store_alert(alert_data)
    forward_to_discord(alert_data, claude_analysis, claude_confidence)

    return jsonify({
        "status": "ok",
        "alert_type": alert_data["alert_type"],
        "claude_confidence": claude_confidence
    })


@app.route("/test-webhook", methods=["POST"])
def test_webhook():
    """Test endpoint — sends to Discord but does NOT store in database"""
    try:
        raw = request.get_json(force=True)
    except Exception:
        try:
            raw = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400

    alert_data = parse_alert(raw)
    recent = get_recent_alerts(10)

    claude_analysis = ""
    claude_confidence = ""
    skip_types = ["NO_ENTRY", "TREND_OVER", "REGIME_SHIFT", "SESSION_OPEN"]
    if alert_data["alert_type"] not in skip_types:
        claude_analysis, claude_confidence = analyze_with_claude(alert_data, recent)

    discord_sent = forward_to_discord(alert_data, claude_analysis, claude_confidence, is_test=True)

    return jsonify({
        "status": "ok",
        "test": True,
        "stored_in_db": False,
        "discord_sent": discord_sent,
        "alert_type": alert_data["alert_type"],
        "parsed_data": {k: v for k, v in alert_data.items() if k != "raw_json"},
        "claude_analysis": claude_analysis,
        "claude_confidence": claude_confidence
    })


@app.route("/alerts", methods=["GET"])
def list_alerts():
    n = request.args.get("n", 20, type=int)
    alerts = get_recent_alerts(n)
    return jsonify(alerts)


@app.route("/alerts/<int:alert_id>", methods=["DELETE"])
def remove_alert(alert_id):
    success = delete_alert(alert_id)
    if success:
        return jsonify({"status": "deleted", "id": alert_id})
    return jsonify({"error": "Alert not found"}), 404


@app.route("/alerts/clear", methods=["POST"])
def clear_alerts():
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Invalid secret"}), 403
    count = clear_all_alerts()
    return jsonify({"status": "cleared", "deleted": count})


@app.route("/stats", methods=["GET"])
def stats():
    days = request.args.get("days", 7, type=int)
    return jsonify(get_stats(days))


@app.route("/sessions", methods=["GET"])
def sessions():
    return jsonify(get_session_stats())


@app.route("/patterns", methods=["GET"])
def patterns():
    """View pattern analysis Claude uses"""
    return jsonify({
        "pattern_analysis": get_pattern_analysis(),
        "session_stats": get_session_stats()
    })


@app.route("/knowledge", methods=["GET"])
def knowledge():
    return jsonify({
        "system_knowledge": SYSTEM_KNOWLEDGE,
        "pattern_analysis": get_pattern_analysis(),
        "session_stats": get_session_stats()
    })


@app.route("/news", methods=["GET"])
def news():
    """View cached news headlines from FinancialJuice"""
    with news_cache_lock:
        items = list(news_cache)
    return jsonify({
        "enabled": ENABLE_NEWS,
        "headlines": items,
        "last_poll": news_last_poll,
        "poll_interval": FJ_POLL_INTERVAL,
        "count": len(items)
    })

@app.route("/weekly-summary", methods=["GET"])
def weekly_summary():
    """Generate weekly performance summary using Claude"""
    alerts = get_recent_alerts(100)
    if not alerts:
        return jsonify({"summary": "No alerts recorded yet."})

    stats_data = get_stats(7)
    session_data = get_session_stats()
    pattern_data = get_pattern_analysis()

    if ANTHROPIC_API_KEY and ENABLE_CLAUDE:
        prompt = f"""{SYSTEM_KNOWLEDGE}

Analyze this week's ES futures trading performance for the OneMP11 system:

Stats: {json.dumps(stats_data)}
Session breakdown: {json.dumps(session_data)}
Pattern analysis: {pattern_data}
Recent alerts: {json.dumps(alerts[:20], default=str)}

Provide:
1) Performance overview (P&L, win rate, streak)
2) Key patterns from DB (which setups worked, which didn't)
3) Session performance (which windows had edge)
4) Claude's own confidence accuracy (was HIGH conf actually winning?)
5) Recommendation for next week

Be concise and actionable. Use specific numbers from the data."""

        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 600,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                return jsonify({
                    "summary": result["content"][0]["text"],
                    "stats": stats_data,
                    "sessions": session_data
                })

        except Exception as e:
            print(f"Weekly summary error: {e}")

    return jsonify({"stats": stats_data, "sessions": session_data, "summary": "Claude unavailable"})


# Start the background news poller
start_news_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

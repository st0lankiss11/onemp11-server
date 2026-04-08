"""
OneMP11 Alert Server V2.2 — Synced with V8.1b (no SL/Smart)
Clean Discord format matching TradingView alert style.
Database-driven Claude analysis + FinancialJuice news.
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
# FINANCIALJUICE NEWS FEED
# ===================================================
FJ_RSS_URL = "https://www.financialjuice.com/feed.ashx?xy=rss"
FJ_POLL_INTERVAL = 30
FJ_MAX_HEADLINES = 20
ENABLE_NEWS = os.environ.get("ENABLE_NEWS", "true").lower() == "true"

news_cache = []
news_cache_lock = threading.Lock()
news_last_poll = 0


def fetch_news():
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
        print(f"NEWS: Fetched {len(headlines)} headlines")
    except Exception as e:
        print(f"NEWS: Error: {e}")


def news_poll_loop():
    while True:
        try:
            fetch_news()
        except Exception as e:
            print(f"NEWS: Poll error: {e}")
        time.sleep(FJ_POLL_INTERVAL)


def start_news_thread():
    if not ENABLE_NEWS:
        print("NEWS: Disabled")
        return
    t = threading.Thread(target=news_poll_loop, daemon=True, name="news-poller")
    t.start()
    print(f"NEWS: Poller started (interval={FJ_POLL_INTERVAL}s)")


def get_news_context(max_items=5):
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
# V8.1b KNOWLEDGE BASE (updated — no SL/Smart)
# ===================================================
SYSTEM_KNOWLEDGE = """
You are the OneMP11 V8.1b trading system analyst for ES futures.

SIGNAL GENERATION:
- Entries require ALL four: CVD Momentum > 60, Histogram > 5, Price above/below Kalman VWAP, Kalman slope confirms direction
- ADX > 20 required for entries and re-entries only
- Reversals fire when ALL four conditions flip — ADX does NOT gate reversals
- Re-entries on momentum flip back to trend (max 3 per trend)
- Exit mode: HOLD UNTIL REVERSAL (no SL, no smart exit/stop — data proved they hurt)

TL SPREAD (ATR-based):
- TIGHT (< 0.5x ATR): Price hugging TL — strong conviction
- RIDING (0.5-1.0x ATR): Normal trend following
- EXTENDED (1.0-2.0x ATR): Stretched — trail tight
- STRETCHED (> 2.0x ATR): Overextended — high reversion risk

SESSION FILTER:
- No-entry zone: 2pm-8pm CT (blocks entries + re-entries, NOT reversals)
- Force close: 4pm CT Mon-Thu (always ON)
- Friday auto-close: 4pm (market closed Fri 4pm - Sun 5pm)

MILESTONE OUTCOME TRACKING:
- System tracks what happens AFTER milestones are hit
- M+ Win row: How many trades that hit +10/+20/+30 ended profitable
- M- Rcvr row: How many trades that hit -15/-25 eventually recovered
- Historical: 100% of trades hitting +10/+20/+30 ended positive (small sample)
- Historical: 75% of trades hitting -15 recovered

ALERT TYPES:
- ENTRY: Fresh long/short — all 4 conditions aligned
- RE_ENTRY: Momentum flipped back to trend after pullback
- REVERSAL: All conditions flipped — exits current AND enters opposite
- SESSION_CLOSE: Force exit at 4pm CT Mon-Thu
- FRIDAY_CLOSE: Force exit at 4pm Friday
- TREND_OVER: Price crossed TL while flat
- MILESTONE_UP: Trade hit +10, +20, or +30 pts
- MILESTONE_DOWN: Trade hit -15 or -25 pts
- MARKET_CHECK: 10am CST daily snapshot
- REGIME_SHIFT: VIX regime changed
- NO_ENTRY: 2pm block started
- SESSION_OPEN: 8pm session open

CONFIDENCE RULES:
- HIGH: Entry/reversal with ADX>25, strong momentum, TL TIGHT/RIDING, RSI 40-60
- MEDIUM: Mostly aligned but one concern (extended TL, fading mom, session risk)
- LOW: Multiple concerns (low ADX, STRETCHED TL, RSI extreme, midday, against VIX)
- ADX value of -1 means "not included in this alert type" — do NOT treat as zero/weak
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
            timestamp TEXT, alert_type TEXT, direction TEXT, price REAL,
            exit_pts REAL, daily_pnl REAL, weekly_pnl REAL, monthly_pnl REAL,
            total_pnl REAL, tl_spread REAL, tl_state TEXT, rsi REAL,
            vix_rsi REAL, compare_rsi REAL, adx REAL, verdict TEXT,
            claude_analysis TEXT, claude_confidence TEXT, raw_json TEXT, session TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE alerts ADD COLUMN session TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


def get_session_from_time(ts_str):
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
        data.get("timestamp", ""), data.get("alert_type", ""),
        data.get("direction", ""), data.get("price", 0),
        data.get("exit_pts", 0), data.get("daily_pnl", 0),
        data.get("weekly_pnl", 0), data.get("monthly_pnl", 0),
        data.get("total_pnl", 0), data.get("tl_spread", 0),
        data.get("tl_state", ""), data.get("rsi", 0),
        data.get("vix_rsi", 0), data.get("compare_rsi", 0),
        data.get("adx", -1), data.get("verdict", ""),
        data.get("claude_analysis", ""), data.get("claude_confidence", ""),
        data.get("raw_json", ""), session
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
        return {"trades": 0, "wins": 0, "losses": 0, "total_pts": 0, "win_rate": 0}

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
        "trades": len(rows), "wins": wins, "losses": losses,
        "total_pts": round(total_pts, 2),
        "avg_pts": round(total_pts / len(rows), 2) if rows else 0,
        "avg_win": round(sum(win_pts) / len(win_pts), 2) if win_pts else 0,
        "avg_loss": round(sum(loss_pts) / len(loss_pts), 2) if loss_pts else 0,
        "win_rate": round(wins / len(rows) * 100, 1) if rows else 0,
        "streak": f"{streak}{streak_dir}"
    }


def get_session_stats():
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

    for v in sessions.values():
        v["win_rate"] = round(v["wins"] / v["trades"] * 100, 1) if v["trades"] > 0 else 0
        v["avg_pts"] = round(v["pts"] / v["trades"], 2) if v["trades"] > 0 else 0
        v["pts"] = round(v["pts"], 2)

    return sessions


def get_similar_trades(alert_data, limit=30):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    direction = alert_data.get("direction", "")
    tl_state = alert_data.get("tl_state", "")
    session = get_session_from_time(alert_data.get("timestamp", ""))

    c.execute("SELECT alert_type, direction, exit_pts, tl_state, rsi, adx, claude_confidence, session FROM alerts WHERE direction = ? AND exit_pts != 0 ORDER BY id DESC LIMIT ?", (direction, limit))
    all_dir = c.fetchall()
    c.execute("SELECT exit_pts FROM alerts WHERE tl_state = ? AND exit_pts != 0 ORDER BY id DESC LIMIT ?", (tl_state, limit))
    tl_trades = c.fetchall()
    c.execute("SELECT exit_pts FROM alerts WHERE session = ? AND exit_pts != 0 ORDER BY id DESC LIMIT ?", (session, limit))
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT alert_type, direction, exit_pts, tl_state, rsi, adx, claude_confidence, session FROM alerts WHERE exit_pts != 0 ORDER BY id DESC LIMIT 50")
    trades = c.fetchall()
    conn.close()

    if not trades:
        return "No trade history yet."

    long_trades = [t for t in trades if t[1] == "LONG"]
    short_trades = [t for t in trades if t[1] == "SHORT"]
    long_wins = sum(1 for t in long_trades if t[2] > 0)
    short_wins = sum(1 for t in short_trades if t[2] > 0)

    tight = [t for t in trades if t[3] == "TIGHT"]
    riding = [t for t in trades if t[3] == "RIDING"]
    extended = [t for t in trades if t[3] == "EXTENDED"]
    stretched = [t for t in trades if t[3] == "STRETCHED"]
    high_adx = [t for t in trades if t[5] and t[5] >= 25]
    low_adx = [t for t in trades if t[5] and 0 < t[5] < 25]

    entries = [t for t in trades if t[0] == "ENTRY"]
    re_entries = [t for t in trades if t[0] == "RE_ENTRY"]
    reversals = [t for t in trades if t[0] == "REVERSAL"]
    high_conf = [t for t in trades if t[6] == "HIGH"]
    med_conf = [t for t in trades if t[6] == "MEDIUM"]

    total_wins = sum(1 for t in trades if t[2] > 0)
    lines = [f"TRADE HISTORY ({len(trades)} recent):"]
    lines.append(f"  Overall: {total_wins}W / {len(trades)-total_wins}L ({round(total_wins/len(trades)*100) if trades else 0}%)")

    if long_trades:
        lines.append(f"  LONG: {long_wins}/{len(long_trades)} ({round(long_wins/len(long_trades)*100)}%)")
    if short_trades:
        lines.append(f"  SHORT: {short_wins}/{len(short_trades)} ({round(short_wins/len(short_trades)*100)}%)")

    for label, group in [("TIGHT", tight), ("RIDING", riding), ("EXTENDED", extended), ("STRETCHED", stretched)]:
        if group:
            w = sum(1 for t in group if t[2] > 0)
            lines.append(f"  {label}: {w}/{len(group)} ({round(w/len(group)*100)}%)")

    if high_adx:
        w = sum(1 for t in high_adx if t[2] > 0)
        lines.append(f"  ADX 25+: {w}/{len(high_adx)} ({round(w/len(high_adx)*100)}%)")
    if low_adx:
        w = sum(1 for t in low_adx if t[2] > 0)
        lines.append(f"  ADX <25: {w}/{len(low_adx)} ({round(w/len(low_adx)*100)}%)")

    for label, group in [("ENTRY", entries), ("RE_ENTRY", re_entries), ("REVERSAL", reversals)]:
        if group:
            w = sum(1 for t in group if t[2] > 0)
            lines.append(f"  {label}: {w}/{len(group)} ({round(w/len(group)*100)}%)")

    if high_conf:
        w = sum(1 for t in high_conf if t[2] > 0)
        lines.append(f"  Claude HIGH: {w}/{len(high_conf)} ({round(w/len(high_conf)*100)}%)")
    if med_conf:
        w = sum(1 for t in med_conf if t[2] > 0)
        lines.append(f"  Claude MED: {w}/{len(med_conf)} ({round(w/len(med_conf)*100)}%)")

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
# ALERT PARSER (synced with V8.1b — fixed price/ADX)
# ===================================================
def parse_alert(raw_json):
    data = {
        "alert_type": "", "direction": "", "price": 0, "exit_pts": 0,
        "daily_pnl": 0, "weekly_pnl": 0, "monthly_pnl": 0, "total_pnl": 0,
        "tl_spread": 0, "tl_state": "", "rsi": 0, "vix_rsi": 0,
        "compare_rsi": 0, "adx": -1, "verdict": "",
        "raw_json": json.dumps(raw_json) if isinstance(raw_json, dict) else str(raw_json),
        "timestamp": datetime.utcnow().isoformat()
    }

    try:
        content = raw_json.get("content", "") if isinstance(raw_json, dict) else ""

        # === ALERT TYPE DETECTION ===
        if "RE-LONG" in content:
            data["alert_type"], data["direction"] = "RE_ENTRY", "LONG"
        elif "RE-SHORT" in content:
            data["alert_type"], data["direction"] = "RE_ENTRY", "SHORT"
        elif "GO LONG" in content and "REVERSAL" not in content:
            data["alert_type"], data["direction"] = "ENTRY", "LONG"
        elif "GO SHORT" in content and "REVERSAL" not in content:
            data["alert_type"], data["direction"] = "ENTRY", "SHORT"
        elif "REVERSAL" in content:
            data["alert_type"] = "REVERSAL"
            if "GO LONG" in content:
                data["direction"] = "LONG"
            elif "GO SHORT" in content:
                data["direction"] = "SHORT"
            else:
                data["direction"] = "LONG" if "Exited SHORT" in content else "SHORT"
        elif "FRIDAY CLOSE" in content:
            data["alert_type"] = "FRIDAY_CLOSE"
            data["direction"] = "LONG" if "Exited LONG" in content else "SHORT"
        elif "4PM CLOSE" in content:
            data["alert_type"] = "SESSION_CLOSE"
            data["direction"] = "LONG" if "Exited LONG" in content else "SHORT"
        elif " up " in content and "pts" in content and ("LONG" in content or "SHORT" in content):
            data["alert_type"] = "MILESTONE_UP"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif " down " in content and "pts" in content and ("LONG" in content or "SHORT" in content):
            data["alert_type"] = "MILESTONE_DOWN"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif "TREND OVER" in content:
            data["alert_type"] = "TREND_OVER"
        elif "MARKET CHECK" in content:
            data["alert_type"] = "MARKET_CHECK"
        elif "REGIME SHIFT" in content:
            data["alert_type"] = "REGIME_SHIFT"
        elif "NO-ENTRY ZONE" in content:
            data["alert_type"] = "NO_ENTRY"
        elif "SESSION OPEN" in content:
            data["alert_type"] = "SESSION_OPEN"

        # === FIELD EXTRACTION ===

        # ES Price — prefer "ES: $6,645.56", then "@ $", then largest 4+ digit $
        es_match = re.search(r'ES:\s*\$(\d[\d,.]+)', content)
        at_match = re.search(r'@\s*\$(\d[\d,.]+)', content)
        if es_match:
            data["price"] = float(es_match.group(1).replace(",", ""))
        elif at_match:
            data["price"] = float(at_match.group(1).replace(",", ""))
        else:
            all_prices = re.findall(r'\$(\d[\d,.]+)', content)
            for p in all_prices:
                val = float(p.replace(",", ""))
                if val > 1000:
                    data["price"] = val
                    break

        # Exit points
        exit_match = re.search(r'(?:Exited \w+\s*)?([+-]?\d+\.?\d*)pts\s*\(', content)
        if exit_match:
            data["exit_pts"] = float(exit_match.group(1))

        # P&L
        for pattern, key in [(r'Today:\s*([+-]?\d+\.?\d*)', "daily_pnl"),
                              (r'Week:\s*([+-]?\d+\.?\d*)', "weekly_pnl"),
                              (r'Month:\s*([+-]?\d+\.?\d*)', "monthly_pnl")]:
            m = re.search(pattern, content)
            if m:
                data[key] = float(m.group(1))

        total_match = re.search(r'Total:\s*([+-]?\d[\d,.]*?)pts', content)
        if total_match:
            data["total_pnl"] = float(total_match.group(1).replace(",", ""))

        # TL Spread
        tl_match = re.search(r'TL:\s*([+-]?\d+\.?\d*)pts?\s*(\w+)', content)
        if tl_match:
            data["tl_spread"] = float(tl_match.group(1))
            data["tl_state"] = tl_match.group(2).upper()

        # RSI
        rsi_match = re.search(r'RSI:\s*(\d+)', content)
        if rsi_match:
            data["rsi"] = float(rsi_match.group(1))

        # VIX RSI
        vix_match = re.search(r'VIX:\s*(\d+)', content)
        if vix_match:
            data["vix_rsi"] = float(vix_match.group(1))

        # Compare RSI
        cl_match = re.search(r'(?:CL|GC|NQ|DX):\s*(\d+)', content)
        if cl_match:
            data["compare_rsi"] = float(cl_match.group(1))

        # ADX — -1 means not present (different from actually being zero)
        adx_match = re.search(r'ADX:\s*(\d+)', content)
        if adx_match:
            data["adx"] = float(adx_match.group(1))
        else:
            data["adx"] = -1

        # Verdict
        for vp in [r'(💪[^\n]+)', r'(⚡[^\n]+)', r'(📈[^\n]+)', r'(🚨[^\n]+)',
                    r'(⚠️[^\n]+)', r'(🔄[^\n]+)', r'(📊[^\n]+)']:
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
    if not ANTHROPIC_API_KEY or not ENABLE_CLAUDE:
        return "", ""

    pattern_context = get_pattern_analysis()
    similar = get_similar_trades(alert_data)
    news_context = get_news_context(max_items=5)

    recent_context = ""
    if recent_alerts:
        recent_context = "\nRecent alerts:\n"
        for a in recent_alerts[:10]:
            pts_tag = f" exit:{a.get('exit_pts', 0):+.1f}pts" if a.get('exit_pts', 0) != 0 else ""
            recent_context += f"  {a.get('alert_type', '')} {a.get('direction', '')}{pts_tag} ({a.get('session', '')})\n"

    similar_context = ""
    if similar["direction_trades"] > 0:
        similar_context = f"\nSIMILAR PATTERNS:\n  {similar['direction_trades']} {alert_data.get('direction', '')} trades: {similar['direction_wr']}% WR\n  {similar['tl_trades']} {similar['tl_state']} entries: {similar['tl_wr']}% WR\n  {similar['session_trades']} {similar['session']} trades: {similar['session_wr']}% WR"

    adx_val = alert_data.get('adx', -1)
    adx_str = "N/A (not in this alert type — do NOT assume zero)" if adx_val == -1 else str(adx_val)

    prompt = f"""{SYSTEM_KNOWLEDGE}

{pattern_context}
{similar_context}
{recent_context}
{news_context}

CURRENT ALERT:
  Type: {alert_data.get('alert_type', '')}
  Direction: {alert_data.get('direction', '')}
  Price: {alert_data.get('price', 0)}
  RSI: {alert_data.get('rsi', 0)}
  ADX: {adx_str}
  VIX RSI: {alert_data.get('vix_rsi', 0)} {"(off hours)" if alert_data.get('vix_rsi', 0) == 0 else ""}
  CL RSI: {alert_data.get('compare_rsi', 0)}
  TL: {alert_data.get('tl_spread', 0)} ({alert_data.get('tl_state', '')})
  Verdict: {alert_data.get('verdict', 'none')}
  Session: {get_session_from_time(alert_data.get('timestamp', ''))}

Brief assessment (2-3 sentences). Reference database patterns with specific numbers.
If news is relevant, note it briefly. End with exactly one of:
[HIGH CONFIDENCE], [MEDIUM CONFIDENCE], or [LOW CONFIDENCE]."""

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
# DISCORD — CLEAN TEXT FORMAT (matches TV alert style)
# ===================================================
ALERT_STYLES = {
    "ENTRY":          {"emoji": "🟢", "color": 5763719,  "label": "ENTRY"},
    "RE_ENTRY":       {"emoji": "🔁", "color": 3447003,  "label": "RE-ENTRY"},
    "SESSION_CLOSE":  {"emoji": "⏸",  "color": 10070709, "label": "4PM CLOSE"},
    "FRIDAY_CLOSE":   {"emoji": "🔒", "color": 10070709, "label": "FRIDAY CLOSE"},
    "REVERSAL":       {"emoji": "🔄", "color": 15844367, "label": "REVERSAL"},
    "TREND_OVER":     {"emoji": "❌", "color": 9807270,  "label": "TREND OVER"},
    "MILESTONE_UP":   {"emoji": "📈", "color": 5763719,  "label": "MILESTONE UP"},
    "MILESTONE_DOWN": {"emoji": "📉", "color": 15548997, "label": "MILESTONE DOWN"},
    "MARKET_CHECK":   {"emoji": "📋", "color": 3447003,  "label": "10am MARKET CHECK"},
    "REGIME_SHIFT":   {"emoji": "🌡️", "color": 16750848, "label": "REGIME SHIFT"},
    "NO_ENTRY":       {"emoji": "⏸",  "color": 16750848, "label": "NO-ENTRY ZONE"},
    "SESSION_OPEN":   {"emoji": "🔔", "color": 3066993,  "label": "SESSION OPEN"},
    "PARSE_ERROR":    {"emoji": "❓", "color": 9807270,  "label": "UNKNOWN"},
}


def build_discord_payload(alert_data, claude_analysis="", claude_confidence="", is_test=False):
    """Build clean text-based Discord embeds matching TradingView alert style"""
    atype = alert_data.get("alert_type", "")
    style = ALERT_STYLES.get(atype, ALERT_STYLES["PARSE_ERROR"])
    direction = alert_data.get("direction", "")
    price = alert_data.get("price", 0)

    # Direction styling
    dir_emoji = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else ""
    dir_arrow = "↑" if direction == "LONG" else "↓" if direction == "SHORT" else ""
    dir_label = f"{direction} {dir_arrow}" if direction else ""

    # Color: green for long entries, red for short entries/exits
    if atype in ("ENTRY", "RE_ENTRY", "MILESTONE_UP") and direction == "LONG":
        color = 5763719  # green
    elif atype in ("ENTRY", "RE_ENTRY", "MILESTONE_UP") and direction == "SHORT":
        color = 15548997  # red
    elif atype in ("MILESTONE_DOWN",):
        color = 15548997  # red
    elif atype == "REVERSAL":
        color = 5763719 if direction == "LONG" else 15548997
    else:
        color = style["color"]

    # === BUILD MAIN EMBED AS CLEAN TEXT ===
    lines = []

    # Header
    test_prefix = "🧪 TEST — " if is_test else ""
    lines.append(f"{test_prefix}{style['emoji']} **{style['label']}**")

    # Direction + Price
    if direction and price:
        price_str = f"${price:,.2f}"
        lines.append(f"")
        lines.append(f"{dir_emoji} **{dir_label}** │ ES @ **{price_str}**")

    # Verdict
    verdict = alert_data.get("verdict", "")
    if verdict:
        lines.append(f"│  {verdict}")

    # Exit P&L (for exits/reversals)
    exit_pts = alert_data.get("exit_pts", 0)
    if exit_pts:
        e_emoji = "✅" if exit_pts > 0 else "❌"
        lines.append(f"")
        lines.append(f"{e_emoji} **{exit_pts:+.1f} pts**")

    # Technicals block
    rsi = alert_data.get("rsi", 0)
    vix = alert_data.get("vix_rsi", 0)
    cl = alert_data.get("compare_rsi", 0)
    adx = alert_data.get("adx", -1)

    if rsi or vix or cl or adx > 0:
        lines.append("")
        lines.append("📊 **Technicals**")

        tech_parts = []
        if rsi:
            rsi_dot = "🟢" if 40 <= rsi <= 60 else "🟡" if 30 <= rsi <= 70 else "🔴"
            tech_parts.append(f"RSI: {rsi_dot} {rsi:.0f}")
        if vix:
            vix_dot = "🔴" if vix > 60 else "🟡" if vix > 40 else "🟢"
            tech_parts.append(f"VIX: {vix_dot} {vix:.0f}")
        if tech_parts:
            lines.append(" │ ".join(tech_parts))

        tech_parts2 = []
        if cl:
            tech_parts2.append(f"CL: {cl:.0f}")
        if adx > 0:
            adx_icon = "💪" if adx >= 25 else "💤" if adx < 20 else ""
            tech_parts2.append(f"ADX: {adx_icon} {adx:.0f}")
        if tech_parts2:
            lines.append(" │ ".join(tech_parts2))

    # TL Spread
    tl_spread = alert_data.get("tl_spread", 0)
    tl_state = alert_data.get("tl_state", "")
    if tl_spread or tl_state:
        tl_emoji = "📈" if tl_spread >= 0 else "📉"
        state_icon = {"TIGHT": "🎯", "RIDING": "🏄", "EXTENDED": "⚡", "STRETCHED": "🚨"}.get(tl_state, "")
        lines.append("")
        lines.append(f"{tl_emoji} **TL Spread**")
        lines.append(f"**{tl_spread:+.1f} pts** — {state_icon} {tl_state}")

    # P&L summary (for exits)
    daily = alert_data.get("daily_pnl", 0)
    weekly = alert_data.get("weekly_pnl", 0)
    monthly = alert_data.get("monthly_pnl", 0)
    total = alert_data.get("total_pnl", 0)

    if daily or weekly or total:
        lines.append("")
        lines.append("💰 **P&L**")
        pnl_parts = []
        if daily:
            pnl_parts.append(f"Today: **{daily:+.1f}**")
        if weekly:
            pnl_parts.append(f"Week: **{weekly:+.1f}**")
        if pnl_parts:
            lines.append(" │ ".join(pnl_parts))
        pnl_parts2 = []
        if monthly:
            pnl_parts2.append(f"Month: **{monthly:+.1f}**")
        if total:
            pnl_parts2.append(f"Total: **{total:+.1f} pts**")
        if pnl_parts2:
            lines.append(" │ ".join(pnl_parts2))

    description = "\n".join(lines)

    main_embed = {
        "description": description,
        "color": color,
        "footer": {"text": "⚡ © 2026 OneMP11 V8.1b"},
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    }

    embeds = [main_embed]

    # === CLAUDE ANALYSIS EMBED ===
    if claude_analysis:
        if claude_confidence == "HIGH":
            c_color, c_emoji, c_label = 5763719, "🟢", "HIGH CONFIDENCE"
        elif claude_confidence == "LOW":
            c_color, c_emoji, c_label = 15548997, "🔴", "LOW CONFIDENCE"
        else:
            c_color, c_emoji, c_label = 16750848, "🟡", "MEDIUM CONFIDENCE"

        claude_lines = []
        claude_lines.append("🤖 **Claude Analysis**")
        claude_lines.append("")
        claude_lines.append(f"{c_emoji} **{c_label}**")
        claude_lines.append("────────────────────")
        claude_lines.append(claude_analysis)

        claude_embed = {
            "description": "\n".join(claude_lines),
            "color": c_color,
        }
        embeds.append(claude_embed)

    return {"embeds": embeds, "username": "OneMP11"}


def forward_to_discord(alert_data, claude_analysis="", claude_confidence="", is_test=False):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD: No webhook URL")
        return False

    try:
        payload = build_discord_payload(alert_data, claude_analysis, claude_confidence, is_test)
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"DISCORD: {response.status_code}")
        if response.status_code not in [200, 204]:
            print(f"DISCORD: Error: {response.text[:500]}")
            return False
        return True
    except Exception as e:
        print(f"DISCORD: {e}")
        return False


# ===================================================
# ROUTES
# ===================================================
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "running", "service": "OneMP11 Alert Server",
        "version": "2.2 (V8.1b synced)", "claude": ENABLE_CLAUDE,
        "discord": bool(DISCORD_WEBHOOK_URL), "stats_7d": get_stats(7)
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.get_json(force=True)
    except Exception:
        try:
            raw = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400

    alert_data = parse_alert(raw)
    recent = get_recent_alerts(10)

    claude_analysis, claude_confidence = "", ""
    skip_types = ["NO_ENTRY", "TREND_OVER", "REGIME_SHIFT", "SESSION_OPEN"]
    if alert_data["alert_type"] not in skip_types:
        claude_analysis, claude_confidence = analyze_with_claude(alert_data, recent)

    alert_data["claude_analysis"] = claude_analysis
    alert_data["claude_confidence"] = claude_confidence

    store_alert(alert_data)
    forward_to_discord(alert_data, claude_analysis, claude_confidence)

    return jsonify({"status": "ok", "type": alert_data["alert_type"], "confidence": claude_confidence})


@app.route("/test-webhook", methods=["POST"])
def test_webhook():
    try:
        raw = request.get_json(force=True)
    except Exception:
        try:
            raw = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400

    alert_data = parse_alert(raw)
    recent = get_recent_alerts(10)

    claude_analysis, claude_confidence = "", ""
    skip_types = ["NO_ENTRY", "TREND_OVER", "REGIME_SHIFT", "SESSION_OPEN"]
    if alert_data["alert_type"] not in skip_types:
        claude_analysis, claude_confidence = analyze_with_claude(alert_data, recent)

    discord_sent = forward_to_discord(alert_data, claude_analysis, claude_confidence, is_test=True)
    return jsonify({
        "status": "ok", "test": True, "stored": False, "discord": discord_sent,
        "type": alert_data["alert_type"],
        "parsed": {k: v for k, v in alert_data.items() if k != "raw_json"},
        "claude": claude_analysis, "confidence": claude_confidence
    })


@app.route("/alerts", methods=["GET"])
def list_alerts():
    return jsonify(get_recent_alerts(request.args.get("n", 20, type=int)))


@app.route("/alerts/<int:alert_id>", methods=["DELETE"])
def remove_alert(alert_id):
    if delete_alert(alert_id):
        return jsonify({"status": "deleted", "id": alert_id})
    return jsonify({"error": "Not found"}), 404


@app.route("/alerts/clear", methods=["POST"])
def clear_alerts():
    if request.args.get("secret", "") != WEBHOOK_SECRET:
        return jsonify({"error": "Invalid secret"}), 403
    return jsonify({"status": "cleared", "deleted": clear_all_alerts()})


@app.route("/stats", methods=["GET"])
def stats():
    return jsonify(get_stats(request.args.get("days", 7, type=int)))


@app.route("/sessions", methods=["GET"])
def sessions():
    return jsonify(get_session_stats())


@app.route("/patterns", methods=["GET"])
def patterns():
    return jsonify({"patterns": get_pattern_analysis(), "sessions": get_session_stats()})


@app.route("/knowledge", methods=["GET"])
def knowledge():
    return jsonify({"knowledge": SYSTEM_KNOWLEDGE, "patterns": get_pattern_analysis()})


@app.route("/news", methods=["GET"])
def news():
    with news_cache_lock:
        items = list(news_cache)
    return jsonify({"enabled": ENABLE_NEWS, "headlines": items, "count": len(items)})


@app.route("/weekly-summary", methods=["GET"])
def weekly_summary():
    alerts = get_recent_alerts(100)
    if not alerts:
        return jsonify({"summary": "No alerts yet."})

    stats_data = get_stats(7)
    session_data = get_session_stats()
    pattern_data = get_pattern_analysis()

    if ANTHROPIC_API_KEY and ENABLE_CLAUDE:
        prompt = f"""{SYSTEM_KNOWLEDGE}

Analyze this week's performance:
Stats: {json.dumps(stats_data)}
Sessions: {json.dumps(session_data)}
Patterns: {pattern_data}
Recent: {json.dumps(alerts[:20], default=str)}

Provide: 1) P&L overview 2) Best/worst setups 3) Session edge 4) Claude accuracy 5) Next week recommendation.
Be concise, use specific numbers."""

        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]},
                timeout=30
            )
            if response.status_code == 200:
                return jsonify({"summary": response.json()["content"][0]["text"], "stats": stats_data, "sessions": session_data})
        except Exception as e:
            print(f"Weekly summary error: {e}")

    return jsonify({"stats": stats_data, "sessions": session_data, "summary": "Claude unavailable"})


start_news_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

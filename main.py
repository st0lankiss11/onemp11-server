"""
OneMP11 Alert Server V2 — Database-Driven Intelligence
No external market data dependencies. Claude learns from YOUR alert history.
Deploy on Railway: https://railway.app
"""

import os
import json
import re
import sqlite3
import time
import traceback
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
# ONEMP11 V8.1b KNOWLEDGE BASE
# Claude uses this to understand every alert type
# ===================================================
SYSTEM_KNOWLEDGE = """
You are the OneMP11 V8.1b trading system analyst for ES futures.
You have deep knowledge of this specific system's signal logic:

SIGNAL GENERATION:
- Entries require ALL four conditions: CVD Momentum > Strong Threshold,
  Histogram > Min Level, Price above/below Kalman VWAP, Kalman slope confirms direction
- ADX must be above threshold (trending market) for entries and re-entries
- Reversals fire when ALL conditions flip to opposite direction (ADX does NOT gate reversals)
- Re-entries fire on momentum flip back to trend direction (max 3 per trend)
- Session close forces exit at configured hour (always on Fridays at 4pm CT)

KALMAN VWAP:
- Higher-order Kalman filter smoothing price + VWAP blend
- Slope > Min Slope Strength = bullish, < -Min Slope Strength = bearish
- Price must be on correct side of Kalman line for entry

CVD FLOW (Cumulative Volume Delta):
- Uses 1-min lower timeframe delta for precision during RTH
- Falls back to bar-range approximation during overnight/no-entry zone
- Momentum: normalized ROC of CVD, range -100 to +100
- Histogram (Vol Intensity): volume-weighted delta strength, range -100 to +100

REGIME TUNING (VIX-based):
  Setting              | High VIX(25+) | Normal(15-25) | Low(<15)
  Strong Threshold     | 60            | 55            | 50
  Kalman VWAP PN       | 0.08          | 0.05          | 0.03
  CVD Kalman PN        | 0.08          | 0.05          | 0.03
  Min Slope Strength   | 0.1           | 0.1           | 0.05
  No-Entry Start       | 14 (2pm)      | 15 (3pm)      | 16 (4pm)
  ADX Threshold        | 20            | 20            | 18
  Histogram Min        | 5             | 5             | 3

ALERT TYPES:
- ENTRY: Fresh long/short when all conditions align (strongest signal)
- RE-ENTRY (RE-LONG/RE-SHORT): Momentum flipped back to trend after pullback
- REVERSAL: All conditions flipped - exits current trade AND enters opposite
- SESSION_CLOSE: Forced exit at configured hour (4pm CT default)
- FRIDAY_CLOSE: Always force-close at 4pm Friday (market closed Fri 4pm - Sun 5pm)
- TREND_OVER: Price crossed Kalman line while flat - no longer watching
- MILESTONE_UP: Open trade hit profit target (+10, +20, +30 pts)
- MILESTONE_DOWN: Open trade hit loss warning (-15 pts) or danger (-25 pts)
- MARKET_CHECK: 10am CST daily snapshot of conditions
- REGIME_SHIFT: VIX regime changed (e.g., NORMAL -> HIGH)
- NO_ENTRY: Power hour block started (2pm-8pm CT default in high VIX)
- SESSION_OPEN: New trading session started with key levels

TL SPREAD CLASSIFICATION (ATR-based):
- TIGHT (< 0.5 ATR): Price hugging trend line - strong conviction zone
- RIDING (0.5-1.0 ATR): Normal trend following distance
- EXTENDED (1.0-2.0 ATR): Getting stretched - trail tight
- STRETCHED (> 2.0 ATR): Overextended - high reversion risk

RSI CONTEXT:
- ES RSI: Main instrument relative strength
- VIX RSI: Fear gauge momentum (only valid during RTH 8am-3pm CT)
- Compare RSI (CL/crude): Cross-market confirmation

SESSION WINDOWS (CST):
- Open: 8-10am (highest edge historically)
- Midday: 10am-12pm (chop zone)
- Afternoon: 12-2pm (post-lunch continuation)
- Power Hour: 2-4pm (volatile, entries blocked in high VIX)
- Overnight: 4pm-8am (thinner, wider stops needed)

CONFIDENCE ASSESSMENT RULES:
- HIGH: Entry/reversal with ADX>25, strong momentum, TL TIGHT/RIDING, RSI not extreme
- MEDIUM: Conditions mostly aligned but one concern (extended TL, fading momentum, etc.)
- LOW: Multiple concerns (low ADX, stretched TL, RSI extreme, against VIX trend)
- For milestones UP: assess hold vs. take-profit based on trend strength + TL spread
- For milestones DOWN: assess cut vs. hold based on trend integrity
- Session context matters: Open session trades have highest historical edge
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
            raw_json TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()


def store_alert(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO alerts (
            timestamp, alert_type, direction, price, exit_pts,
            daily_pnl, weekly_pnl, monthly_pnl, total_pnl,
            tl_spread, tl_state, rsi, vix_rsi, compare_rsi,
            adx, verdict, claude_analysis, claude_confidence, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        data.get("raw_json", "")
    ))
    conn.commit()
    conn.close()


def get_recent_alerts(n=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (n,))
    rows = c.fetchall()
    conn.close()
    columns = [
        "id", "timestamp", "alert_type", "direction", "price", "exit_pts",
        "daily_pnl", "weekly_pnl", "monthly_pnl", "total_pnl",
        "tl_spread", "tl_state", "rsi", "vix_rsi", "compare_rsi",
        "adx", "verdict", "claude_analysis", "claude_confidence", "raw_json"
    ]
    return [dict(zip(columns, row)) for row in rows]


def get_stats(days=7):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    c.execute("""
        SELECT alert_type, direction, exit_pts, claude_confidence
        FROM alerts WHERE timestamp > ? AND exit_pts != 0
        ORDER BY id DESC
    """, (cutoff,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"trades": 0, "wins": 0, "losses": 0, "total_pts": 0, "avg_pts": 0, "streak": ""}

    wins = sum(1 for r in rows if r[2] > 0)
    losses = sum(1 for r in rows if r[2] < 0)
    total_pts = sum(r[2] for r in rows)

    streak = 0
    streak_dir = ""
    for r in rows:
        if r[2] > 0:
            if streak_dir == "" or streak_dir == "W":
                streak += 1
                streak_dir = "W"
            else:
                break
        elif r[2] < 0:
            if streak_dir == "" or streak_dir == "L":
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
        "win_rate": round(wins / len(rows) * 100, 1) if rows else 0,
        "streak": f"{streak}{streak_dir}"
    }


def get_session_stats():
    """Get performance breakdown by session window"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, alert_type, direction, exit_pts, claude_confidence
        FROM alerts WHERE exit_pts != 0
        ORDER BY id DESC LIMIT 200
    """)
    rows = c.fetchall()
    conn.close()

    sessions = {
        "open_8_10": {"trades": 0, "wins": 0, "pts": 0},
        "midday_10_12": {"trades": 0, "wins": 0, "pts": 0},
        "afternoon_12_14": {"trades": 0, "wins": 0, "pts": 0},
        "power_14_16": {"trades": 0, "wins": 0, "pts": 0},
        "overnight": {"trades": 0, "wins": 0, "pts": 0},
    }

    for row in rows:
        ts_str = row[0]
        pts = row[3]
        try:
            dt = datetime.fromisoformat(ts_str)
            h = dt.hour
        except Exception:
            h = 12

        if 8 <= h < 10:
            key = "open_8_10"
        elif 10 <= h < 12:
            key = "midday_10_12"
        elif 12 <= h < 14:
            key = "afternoon_12_14"
        elif 14 <= h < 16:
            key = "power_14_16"
        else:
            key = "overnight"

        sessions[key]["trades"] += 1
        sessions[key]["pts"] += pts
        if pts > 0:
            sessions[key]["wins"] += 1

    for k, v in sessions.items():
        v["win_rate"] = round(v["wins"] / v["trades"] * 100, 1) if v["trades"] > 0 else 0
        v["avg_pts"] = round(v["pts"] / v["trades"], 2) if v["trades"] > 0 else 0
        v["pts"] = round(v["pts"], 2)

    return sessions


def get_pattern_analysis():
    """Analyze patterns in alert history for Claude context"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT alert_type, direction, exit_pts, tl_state, rsi, adx, claude_confidence
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
    extended_trades = [t for t in trades if t[3] == "EXTENDED"]

    high_adx = [t for t in trades if t[5] and t[5] >= 25]
    low_adx = [t for t in trades if t[5] and t[5] < 25]

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

    lines = []
    total_wins = sum(1 for t in trades if t[2] > 0)
    total_losses = sum(1 for t in trades if t[2] < 0)
    lines.append(f"TRADE HISTORY ({len(trades)} recent trades):")
    lines.append(f"  Overall: {total_wins}W / {total_losses}L")
    lines.append(f"  Current streak: {streak}{streak_dir}")

    if long_trades:
        pct = round(long_wins / len(long_trades) * 100)
        lines.append(f"  LONG: {long_wins}/{len(long_trades)} wins ({pct}%)")
    if short_trades:
        pct = round(short_wins / len(short_trades) * 100)
        lines.append(f"  SHORT: {short_wins}/{len(short_trades)} wins ({pct}%)")

    if tight_trades:
        tw = sum(1 for t in tight_trades if t[2] > 0)
        lines.append(f"  TIGHT entries: {tw}/{len(tight_trades)} wins")
    if extended_trades:
        ew = sum(1 for t in extended_trades if t[2] > 0)
        lines.append(f"  EXTENDED entries: {ew}/{len(extended_trades)} wins")

    if high_adx:
        haw = sum(1 for t in high_adx if t[2] > 0)
        lines.append(f"  High ADX (25+): {haw}/{len(high_adx)} wins")
    if low_adx:
        law = sum(1 for t in low_adx if t[2] > 0)
        lines.append(f"  Low ADX (<25): {law}/{len(low_adx)} wins")

    if high_conf:
        lines.append(f"  Claude HIGH conf accuracy: {high_conf_wins}/{len(high_conf)} wins")

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
# ALERT PARSER
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

        if "GO LONG" in content or "RE-LONG" in content:
            data["alert_type"] = "ENTRY"
            data["direction"] = "LONG"
        elif "GO SHORT" in content or "RE-SHORT" in content:
            data["alert_type"] = "ENTRY"
            data["direction"] = "SHORT"
        elif "4PM CLOSE" in content or "FRIDAY CLOSE" in content:
            data["alert_type"] = "SESSION_CLOSE"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif "REVERSAL" in content:
            data["alert_type"] = "REVERSAL"
            data["direction"] = "LONG" if "GO LONG" in content else "SHORT"
        elif "TREND OVER" in content:
            data["alert_type"] = "TREND_OVER"
        elif "up " in content and "pts" in content:
            data["alert_type"] = "MILESTONE_UP"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif "down " in content and "pts" in content:
            data["alert_type"] = "MILESTONE_DOWN"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif "10am MARKET CHECK" in content:
            data["alert_type"] = "MARKET_CHECK"
        elif "REGIME SHIFT" in content:
            data["alert_type"] = "REGIME_SHIFT"
        elif "NO-ENTRY ZONE" in content:
            data["alert_type"] = "NO_ENTRY"
        elif "SESSION OPEN" in content:
            data["alert_type"] = "SESSION_OPEN"

        price_match = re.search(r'\$(\d[\d,.]*)', content)
        if price_match:
            data["price"] = float(price_match.group(1).replace(",", ""))

        exit_match = re.search(r'([+-]?\d+\.?\d*)pts\s*\(', content)
        if exit_match:
            data["exit_pts"] = float(exit_match.group(1))

        today_match = re.search(r'Today:\s*([+-]?\d+\.?\d*)', content)
        if today_match:
            data["daily_pnl"] = float(today_match.group(1))

        week_match = re.search(r'Week:\s*([+-]?\d+\.?\d*)', content)
        if week_match:
            data["weekly_pnl"] = float(week_match.group(1))

        month_match = re.search(r'Month:\s*([+-]?\d+\.?\d*)', content)
        if month_match:
            data["monthly_pnl"] = float(month_match.group(1))

        total_match = re.search(r'Total:\s*([+-]?\d+\.?\d*)pts', content)
        if total_match:
            data["total_pnl"] = float(total_match.group(1))

        tl_match = re.search(r'TL:\s*([+-]?\d+\.?\d*)pts\s*(\w+)', content)
        if tl_match:
            data["tl_spread"] = float(tl_match.group(1))
            data["tl_state"] = tl_match.group(2)

        rsi_match = re.search(r'RSI:\s*(\d+)', content)
        if rsi_match:
            data["rsi"] = float(rsi_match.group(1))

        vix_match = re.search(r'VIX:\s*(\d+)', content)
        if vix_match:
            data["vix_rsi"] = float(vix_match.group(1))

        cl_match = re.search(r'CL:\s*(\d+)', content)
        if cl_match:
            data["compare_rsi"] = float(cl_match.group(1))

        adx_match = re.search(r'ADX:\s*(\d+)', content)
        if adx_match:
            data["adx"] = float(adx_match.group(1))

    except Exception as e:
        data["alert_type"] = "PARSE_ERROR"
        data["verdict"] = str(e)

    return data


# ===================================================
# CLAUDE ANALYSIS (database-driven, no Yahoo Finance)
# ===================================================
def analyze_with_claude(alert_data, recent_alerts):
    """Send alert + database history context to Claude for analysis"""
    if not ANTHROPIC_API_KEY or not ENABLE_CLAUDE:
        return "", ""

    pattern_context = get_pattern_analysis()

    recent_context = ""
    if recent_alerts:
        recent_context = "\nRecent alerts (newest first):\n"
        for a in recent_alerts[:10]:
            conf_tag = f" [Claude: {a.get('claude_confidence', '')}]" if a.get('claude_confidence') else ""
            pts_tag = f" exit:{a.get('exit_pts', 0):+.1f}pts" if a.get('exit_pts', 0) != 0 else ""
            recent_context += f"  {a.get('alert_type', '')} {a.get('direction', '')} @ {a.get('price', 0)}{pts_tag}{conf_tag}\n"

    prompt = f"""{SYSTEM_KNOWLEDGE}

DATABASE CONTEXT (your trade history):
{pattern_context}

{recent_context}

CURRENT ALERT TO ANALYZE:
  Type: {alert_data.get('alert_type', '')}
  Direction: {alert_data.get('direction', '')}
  Price: {alert_data.get('price', 0)}
  RSI: {alert_data.get('rsi', 0)}
  ADX: {alert_data.get('adx', 0)}
  VIX RSI: {alert_data.get('vix_rsi', 0)}
  CL RSI: {alert_data.get('compare_rsi', 0)}
  TL Spread: {alert_data.get('tl_spread', 0)} ({alert_data.get('tl_state', '')})
  Verdict from TradingView: {alert_data.get('verdict', '')}

Provide a brief technical assessment (2-3 sentences max).
Reference your database history when relevant (e.g. "LONG entries from TIGHT have been winning at 75%").
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
    "ENTRY":          {"emoji": "\U0001f7e2", "color": 3066993,  "label": "ENTRY SIGNAL"},
    "SESSION_CLOSE":  {"emoji": "\U0001f3c1", "color": 10070709, "label": "SESSION CLOSE"},
    "REVERSAL":       {"emoji": "\U0001f504", "color": 15844367, "label": "REVERSAL"},
    "TREND_OVER":     {"emoji": "\U0001f6d1", "color": 10038562, "label": "TREND OVER"},
    "MILESTONE_UP":   {"emoji": "\U0001f4c8", "color": 3066993,  "label": "MILESTONE UP"},
    "MILESTONE_DOWN": {"emoji": "\U0001f4c9", "color": 15158332, "label": "MILESTONE DOWN"},
    "MARKET_CHECK":   {"emoji": "\U0001f50d", "color": 3447003,  "label": "MARKET CHECK"},
    "REGIME_SHIFT":   {"emoji": "\u26a0\ufe0f",  "color": 15844367, "label": "REGIME SHIFT"},
    "NO_ENTRY":       {"emoji": "\U0001f6ab", "color": 10038562, "label": "NO-ENTRY ZONE"},
    "SESSION_OPEN":   {"emoji": "\U0001f514", "color": 3447003,  "label": "SESSION OPEN"},
    "PARSE_ERROR":    {"emoji": "\u2753",     "color": 9807270,  "label": "UNKNOWN ALERT"},
}


def build_discord_embed(alert_data, claude_analysis="", claude_confidence="", is_test=False):
    """Build a rich Discord embed for the alert"""
    atype = alert_data.get("alert_type", "")
    style = ALERT_STYLES.get(atype, ALERT_STYLES["PARSE_ERROR"])

    direction = alert_data.get("direction", "")
    if direction == "LONG":
        dir_emoji = "\U0001f7e2"
        dir_label = "LONG \u2191"
    elif direction == "SHORT":
        dir_emoji = "\U0001f534"
        dir_label = "SHORT \u2193"
    else:
        dir_emoji = "\u26aa"
        dir_label = "\u2014"

    if atype == "ENTRY" and direction == "SHORT":
        style = {**style, "color": 15158332}

    title = f"{style['emoji']} {style['label']}"
    if is_test:
        title = f"\U0001f9ea TEST \u2014 {title}"

    price = alert_data.get("price", 0)
    price_str = f"${price:,.2f}" if price else "\u2014"
    desc_lines = []
    if direction:
        desc_lines.append(f"## {dir_emoji} {dir_label} \u2502 ES @ {price_str}")
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
        fields.append({"name": "\u200b", "value": "**\U0001f4ca Technicals**", "inline": False})
        if rsi or adx:
            rsi_bar = "\U0001f7e2" if 40 <= rsi <= 60 else "\U0001f7e1" if 30 <= rsi <= 70 else "\U0001f534"
            adx_bar = "\U0001f4aa" if adx >= 25 else "\U0001f4a4"
            fields.append({"name": "RSI", "value": f"{rsi_bar} **{rsi:.0f}**", "inline": True})
            fields.append({"name": "ADX", "value": f"{adx_bar} **{adx:.0f}**", "inline": True})
            fields.append({"name": "\u200b", "value": "\u200b", "inline": True})
        if vix or cl:
            vix_emoji = "\U0001f534" if vix > 60 else "\U0001f7e1" if vix > 40 else "\U0001f7e2"
            fields.append({"name": "VIX RSI", "value": f"{vix_emoji} **{vix:.0f}**", "inline": True})
            fields.append({"name": "CL RSI", "value": f"**{cl:.0f}**", "inline": True})
            fields.append({"name": "\u200b", "value": "\u200b", "inline": True})

    # TL Spread
    tl_spread = alert_data.get("tl_spread", 0)
    tl_state = alert_data.get("tl_state", "")
    if tl_spread or tl_state:
        tl_emoji = "\U0001f4c8" if tl_spread >= 0 else "\U0001f4c9"
        fields.append({"name": f"{tl_emoji} TL Spread", "value": f"**{tl_spread:+.1f} pts** \u2014 {tl_state}", "inline": False})

    # P&L
    daily = alert_data.get("daily_pnl", 0)
    weekly = alert_data.get("weekly_pnl", 0)
    total = alert_data.get("total_pnl", 0)
    exit_pts = alert_data.get("exit_pts", 0)

    if daily or weekly or total or exit_pts:
        fields.append({"name": "\u200b", "value": "**\U0001f4b0 P&L**", "inline": False})
        if daily or weekly:
            d_emoji = "\u2705" if daily >= 0 else "\u274c"
            w_emoji = "\u2705" if weekly >= 0 else "\u274c"
            fields.append({"name": "Today / Week", "value": f"{d_emoji} **{daily:+.1f} pts**  {w_emoji} **{weekly:+.1f} pts**", "inline": False})
        if total:
            t_emoji = "\U0001f3c6" if total >= 0 else "\U0001f4c9"
            fields.append({"name": "Total", "value": f"{t_emoji} **{total:+.1f} pts**", "inline": False})
        if exit_pts:
            e_emoji = "\u2705" if exit_pts > 0 else "\u274c"
            fields.append({"name": "Exit", "value": f"{e_emoji} **{exit_pts:+.1f} pts**", "inline": False})

    embed = {
        "title": title,
        "description": description,
        "color": style["color"],
        "fields": fields,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    embeds = [embed]

    if claude_analysis:
        if claude_confidence == "HIGH":
            c_color, c_emoji, c_label = 3066993, "\U0001f7e2", "HIGH CONFIDENCE"
        elif claude_confidence == "MEDIUM":
            c_color, c_emoji, c_label = 15844367, "\U0001f7e1", "MEDIUM CONFIDENCE"
        else:
            c_color, c_emoji, c_label = 15158332, "\U0001f534", "LOW CONFIDENCE"

        claude_embed = {
            "author": {"name": "\U0001f916 Claude Analysis (DB-Trained)"},
            "description": f"{c_emoji} **{c_label}**\n\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n{claude_analysis}",
            "color": c_color,
        }
        if is_test:
            claude_embed["footer"] = {"text": "\U0001f9ea TEST ALERT \u2014 not stored in database"}
        embeds.append(claude_embed)
    elif is_test:
        embed["footer"] = {"text": "\U0001f9ea TEST ALERT \u2014 not stored in database"}

    return embeds


def forward_to_discord(alert_data, claude_analysis="", claude_confidence="", is_test=False):
    """Forward alert to Discord as rich embed"""
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD: No webhook URL configured")
        return False

    try:
        embeds = build_discord_embed(alert_data, claude_analysis, claude_confidence, is_test)
        payload = {"embeds": embeds, "username": "OneMP11"}

        print(f"DISCORD: Sending {len(embeds)} embed(s) to Discord...")
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"DISCORD: Response status={response.status_code}")
        if response.status_code not in [200, 204]:
            print(f"DISCORD: Error response: {response.text[:500]}")
            return False
        return True

    except Exception as e:
        print(f"DISCORD: Exception sending to Discord: {e}")
        traceback.print_exc()
        return False


# ===================================================
# ROUTES
# ===================================================
@app.route("/", methods=["GET"])
def health():
    """Health check"""
    stats = get_stats(7)
    return jsonify({
        "status": "running",
        "service": "OneMP11 Alert Server V2",
        "version": "2.0 (DB-Trained)",
        "claude_enabled": ENABLE_CLAUDE,
        "discord_configured": bool(DISCORD_WEBHOOK_URL),
        "market_data": "removed (database-driven)",
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
    skip_types = ["NO_ENTRY", "TREND_OVER", "REGIME_SHIFT"]
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
    """Test endpoint - sends to Discord but does NOT store in database"""
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
    skip_types = ["NO_ENTRY", "TREND_OVER", "REGIME_SHIFT"]
    if alert_data["alert_type"] not in skip_types:
        claude_analysis, claude_confidence = analyze_with_claude(alert_data, recent)

    discord_sent = forward_to_discord(alert_data, claude_analysis, claude_confidence, is_test=True)

    return jsonify({
        "status": "ok",
        "test": True,
        "stored_in_db": False,
        "discord_sent": discord_sent,
        "alert_type": alert_data["alert_type"],
        "claude_analysis": claude_analysis,
        "claude_confidence": claude_confidence
    })


@app.route("/alerts", methods=["GET"])
def list_alerts():
    """View recent alerts"""
    n = request.args.get("n", 20, type=int)
    alerts = get_recent_alerts(n)
    return jsonify(alerts)


@app.route("/alerts/<int:alert_id>", methods=["DELETE"])
def remove_alert(alert_id):
    """Delete a specific alert"""
    success = delete_alert(alert_id)
    if success:
        return jsonify({"status": "deleted", "id": alert_id})
    return jsonify({"error": "Alert not found"}), 404


@app.route("/alerts/clear", methods=["POST"])
def clear_alerts():
    """Clear all alerts (requires webhook secret)"""
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Invalid secret"}), 403
    count = clear_all_alerts()
    return jsonify({"status": "cleared", "deleted": count})


@app.route("/stats", methods=["GET"])
def stats():
    """Get trading stats"""
    days = request.args.get("days", 7, type=int)
    return jsonify(get_stats(days))


@app.route("/sessions", methods=["GET"])
def sessions():
    """Get session performance breakdown"""
    return jsonify(get_session_stats())


@app.route("/knowledge", methods=["GET"])
def knowledge():
    """View the system knowledge base Claude uses"""
    return jsonify({
        "system_knowledge": SYSTEM_KNOWLEDGE,
        "pattern_analysis": get_pattern_analysis(),
        "session_stats": get_session_stats()
    })


@app.route("/weekly-summary", methods=["GET"])
def weekly_summary():
    """Generate a weekly performance summary using Claude"""
    alerts = get_recent_alerts(100)
    if not alerts:
        return jsonify({"summary": "No alerts recorded yet."})

    stats_data = get_stats(7)
    session_data = get_session_stats()
    pattern_data = get_pattern_analysis()

    if ANTHROPIC_API_KEY and ENABLE_CLAUDE:
        prompt = f"""{SYSTEM_KNOWLEDGE}

Analyze this week's ES futures trading performance:

Stats: {json.dumps(stats_data)}
Session breakdown: {json.dumps(session_data)}
Pattern analysis: {pattern_data}
Recent alerts: {json.dumps(alerts[:20], default=str)}

Provide: 1) Performance overview 2) Key patterns from DB 3) Recommendation for next week. Be concise."""

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
                    "max_tokens": 500,
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

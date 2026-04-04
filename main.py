"""
OneMP11 Alert Server - Intercepts TradingView alerts,
enriches with Claude analysis + live market data, forwards to Discord
Deploy on Railway: https://railway.app
"""

import os
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests
import yfinance as yf

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
# MARKET DATA CACHE
# ===================================================
_market_cache = {"data": None, "timestamp": 0}
_cache_ttl = 300  # 5 minutes

def fetch_market_data():
        """Fetch current market snapshot from Yahoo Finance (free, 15-min delayed)"""
        now = time.time()
        if _market_cache["data"] and (now - _market_cache["timestamp"]) < _cache_ttl:
                    return _market_cache["data"]

        symbols = {
            "ES=F":      "ES Futures",
            "^VIX":      "VIX",
            "SPY":       "SPY",
            "QQQ":       "QQQ",
            "CL=F":      "Crude Oil",
            "^TNX":      "10Y Yield",
            "DX-Y.NYB":  "Dollar Index",
            "GC=F":      "Gold"
        }

    market = {}
    try:
                tickers = yf.Tickers(" ".join(symbols.keys()))
                for sym, name in symbols.items():
                                try:
                                                    info = tickers.tickers[sym].fast_info
                                                    price = getattr(info, "last_price", None)
                                                    prev = getattr(info, "previous_close", None)
                                                    if price and prev:
                                                                            change_pct = round((price - prev) / prev * 100, 2)
                                                                            market[name] = {
                                                                                "price": round(price, 2),
                                                                                "change_pct": change_pct
                                                                            }
                                except Exception:
                                                    pass
    except Exception as e:
                market["_error"] = str(e)

    _market_cache["data"] = market
    _market_cache["timestamp"] = now
    return market

def format_market_context(market):
        """Format market data into a readable string for Claude prompt"""
        if not market or "_error" in market:
                    return "Market data unavailable."
                lines = ["LIVE MARKET SNAPSHOT (15-min delayed):"]
    for name, info in market.items():
                arrow = "^" if info["change_pct"] >= 0 else "v"
                lines.append(f"  {name}: ${info['price']} {arrow} {info['change_pct']}%")
            return "\n".join(lines)

# ===================================================
# DATABASE
# ===================================================
def init_db():
        conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                                    timestamp TEXT NOT NULL,
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

def store_alert(data):
        conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
            INSERT INTO alerts (timestamp, alert_type, direction, price, exit_pts,
                    daily_pnl, weekly_pnl, monthly_pnl, total_pnl, tl_spread, tl_state,
                            rsi, vix_rsi, compare_rsi, adx, verdict, claude_analysis,
                                    claude_confidence, raw_json)
                                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                                """, (
                data.get("timestamp", datetime.utcnow().isoformat()),
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
                "daily_pnl", "weekly_pnl", "monthly_pnl", "total_pnl", "tl_spread",
                "tl_state", "rsi", "vix_rsi", "compare_rsi", "adx", "verdict",
                "claude_analysis", "claude_confidence", "raw_json"
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

def delete_alert(alert_id):
        """Delete a specific alert by ID"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def clear_all_alerts():
        """Clear all alerts from the database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM alerts")
    deleted = c.rowcount
    c.execute("DELETE FROM sqlite_sequence WHERE name='alerts'")
    conn.commit()
    conn.close()
    return deleted

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

        price_match = re.search(r'\$(\d+[\d,.]*)', content)
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

        emoji_list = ["\U0001f4aa", "\u26a1", "\U0001f4c8", "\U0001f6a8", "\u26a0\ufe0f", "\U0001f504", "\U0001f4ca"]
        for v in emoji_list:
                        if v in content:
                                            verdict_match = re.search(f'{re.escape(v)}\\s*(.+?)\\\\n', content)
                                            if not verdict_match:
                                                                    verdict_match = re.search(f'{re.escape(v)}\\s*(.+)', content)
                                                                if verdict_match:
                                                                                        data["verdict"] = verdict_match.group(1).strip()
                                                                                        break

except Exception as e:
        data["alert_type"] = "PARSE_ERROR"
        data["verdict"] = str(e)

    return data

# ===================================================
# CLAUDE ANALYSIS (now with market data)
# ===================================================
def analyze_with_claude(alert_data, recent_alerts):
        """Send alert + live market context to Claude for analysis"""
    if not ANTHROPIC_API_KEY or not ENABLE_CLAUDE:
                return "", ""

    market = fetch_market_data()
    market_context = format_market_context(market)

    recent_summary = ""
    if recent_alerts:
                wins = sum(1 for a in recent_alerts if a.get("exit_pts", 0) > 0)
                losses = sum(1 for a in recent_alerts if a.get("exit_pts", 0) < 0)
                recent_summary = f"""
        Recent alert history (last {len(recent_alerts)} alerts):
        - Wins: {wins}, Losses: {losses}
        - Recent trades: {', '.join([f"{a['alert_type']} {a['direction']} {a.get('exit_pts', 0)}pts" for a in recent_alerts[:5]])}
        """

    prompt = f"""You are a professional ES futures trading analyst with access to live market data.

    Analyze this TradingView alert together with the current market snapshot and provide:
    1. A brief assessment (2-3 sentences max) that incorporates both the alert data AND the broader market context
    2. A confidence level: HIGH, MEDIUM, or LOW

    {market_context}

    Alert data:
    - Type: {alert_data['alert_type']}
    - Direction: {alert_data['direction']}
    - Price: ${alert_data['price']}
    - TL Spread: {alert_data['tl_spread']}pts ({alert_data['tl_state']})
    - RSI: {alert_data['rsi']}
    - VIX RSI: {alert_data['vix_rsi']}
    - CL RSI: {alert_data['compare_rsi']}
    - ADX: {alert_data['adx']}
    - Daily P&L: {alert_data['daily_pnl']}pts
    - Weekly P&L: {alert_data['weekly_pnl']}pts
    {recent_summary}

    Rules:
    - Cross-reference the alert direction with the broader market (VIX level, dollar, yields, oil)
    - For ENTRY alerts: assess if macro conditions support the trade direction
    - For MILESTONE_UP alerts: assess hold vs take profit considering market backdrop
    - For MILESTONE_DOWN alerts: assess hold vs cut considering market backdrop
    - For SESSION_CLOSE/REVERSAL: assess the result and overall market state
    - For SESSION_OPEN/MARKET_CHECK: assess conditions for the upcoming session using full market context
    - Keep it brief and actionable - this goes to a Discord mobile notification
    - Respond in EXACTLY this format:
    ANALYSIS: [your 2-3 sentence analysis]
    CONFIDENCE: [HIGH/MEDIUM/LOW]"""

    try:
                response = requests.post(
                                "https://api.anthropic.com/v1/messages",
                                headers={
                                                    "x-api-key": ANTHROPIC_API_KEY,
                                                    "content-type": "application/json",
                                                    "anthropic-version": "2023-06-01"
                                },
                                json={
                                                    "model": "claude-sonnet-4-20250514",
                                                    "max_tokens": 200,
                                                    "messages": [{"role": "user", "content": prompt}]
                                },
                                timeout=10
                )

        if response.status_code == 200:
                        result = response.json()
                        text = result["content"][0]["text"]

            analysis = ""
            confidence = ""

            analysis_match = re.search(r'ANALYSIS:\s*(.+?)(?=CONFIDENCE:|$)', text, re.DOTALL)
            if analysis_match:
                                analysis = analysis_match.group(1).strip()

            confidence_match = re.search(r'CONFIDENCE:\s*(HIGH|MEDIUM|LOW)', text)
            if confidence_match:
                                confidence = confidence_match.group(1)

            return analysis, confidence
else:
            return f"API error: {response.status_code}", ""
except Exception as e:
        return f"Error: {str(e)}", ""

# ===================================================
# DISCORD FORWARDING
# ===================================================
def forward_to_discord(original_json, claude_analysis="", claude_confidence="", is_test=False):
        """Forward alert to Discord with Claude's analysis appended"""
    if not DISCORD_WEBHOOK_URL:
                return False

    if isinstance(original_json, dict) and "content" in original_json:
                content = original_json["content"]

        # Add TEST label if this is a test alert
                if is_test:
                                content = "\U0001f9ea **[TEST ALERT]**\n" + content

        if claude_analysis:
                        conf_emoji = "\U0001f7e2" if claude_confidence == "HIGH" else "\U0001f7e1" if claude_confidence == "MEDIUM" else "\U0001f534"
                        content += f"\n==================\n\U0001f916 **CLAUDE** {conf_emoji} {claude_confidence}\n{claude_analysis}"

        if is_test:
                        content += "\n\n_\U0001f9ea This was a test alert - not stored in database_"

        original_json["content"] = content

    try:
                response = requests.post(
                                DISCORD_WEBHOOK_URL,
                                json=original_json,
                                timeout=10
                )
                return response.status_code in [200, 204]
except Exception:
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
                "service": "OneMP11 Alert Server",
                "claude_enabled": ENABLE_CLAUDE,
                "discord_configured": bool(DISCORD_WEBHOOK_URL),
                "market_data": "enabled",
                "last_7_days": stats
    })

@app.route("/webhook", methods=["POST"])
def webhook():
        """Receive TradingView webhook, analyze with Claude + market data, forward to Discord"""
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
    forward_to_discord(raw, claude_analysis, claude_confidence)

    return jsonify({
                "status": "ok",
                "alert_type": alert_data["alert_type"],
                "claude_confidence": claude_confidence
    })

@app.route("/test-webhook", methods=["POST"])
def test_webhook():
        """Test endpoint - sends to Discord with Claude analysis but does NOT store in database"""
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

    # Forward to Discord with test flag - but do NOT store in database
    discord_sent = forward_to_discord(raw, claude_analysis, claude_confidence, is_test=True)

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
        """Delete a specific alert by ID"""
    deleted = delete_alert(alert_id)
    if deleted:
                return jsonify({"status": "ok", "deleted_id": alert_id})
            return jsonify({"status": "not_found", "id": alert_id}), 404

@app.route("/alerts/clear", methods=["POST"])
def clear_alerts():
        """Clear all alerts from the database (use with caution)"""
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
                return jsonify({"error": "Invalid secret"}), 403
            deleted = clear_all_alerts()
    return jsonify({"status": "ok", "deleted_count": deleted})

@app.route("/stats", methods=["GET"])
def stats():
        """Get performance stats"""
    days = request.args.get("days", 7, type=int)
    return jsonify(get_stats(days))

@app.route("/market", methods=["GET"])
def market():
        """Get current market snapshot"""
    data = fetch_market_data()
    return jsonify({
                "status": "ok",
                "delayed": "~15 min (Yahoo Finance free tier)",
                "data": data
    })

@app.route("/summary", methods=["GET"])
def weekly_summary():
        """Generate a weekly performance summary using Claude"""
    alerts = get_recent_alerts(100)
    if not alerts:
                return jsonify({"summary": "No alerts recorded yet."})

    stats_data = get_stats(7)

    if ANTHROPIC_API_KEY and ENABLE_CLAUDE:
                market = fetch_market_data()
                market_context = format_market_context(market)

        prompt = f"""Analyze this week's ES futures trading performance and provide a brief summary:

        {market_context}

        Stats (last 7 days):
        - Trades: {stats_data['trades']}
        - Wins: {stats_data['wins']} | Losses: {stats_data['losses']}
        - Win Rate: {stats_data['win_rate']}%
        - Total P&L: {stats_data['total_pts']}pts
        - Avg per trade: {stats_data['avg_pts']}pts
        - Current streak: {stats_data['streak']}

        Provide:
        1. One paragraph performance summary
        2. Key observation (what worked, what didn't)
        3. One actionable suggestion for next week
        Keep it concise - 3-4 sentences total."""

        try:
                        response = requests.post(
                                            "https://api.anthropic.com/v1/messages",
                                            headers={
                                                                    "x-api-key": ANTHROPIC_API_KEY,
                                                                    "content-type": "application/json",
                                                                    "anthropic-version": "2023-06-01"
                                            },
                                            json={
                                                                    "model": "claude-sonnet-4-20250514",
                                                                    "max_tokens": 300,
                                                                    "messages": [{"role": "user", "content": prompt}]
                                            },
                                            timeout=15
                        )
                        if response.status_code == 200:
                                            result = response.json()
                                            summary = result["content"][0]["text"]
                                            return jsonify({"stats": stats_data, "summary": summary})
        except Exception as e:
                        return jsonify({"stats": stats_data, "summary": f"Error generating summary: {e}"})

    return jsonify({"stats": stats_data, "summary": "Claude analysis disabled."})

# ===================================================
# START
# ===================================================
init_db()

if __name__ == "__main__":
        port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

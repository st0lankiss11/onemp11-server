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
DB_PATH             = os.environ.get("DB_PATH", "/data/alerts.db")

# Ensure DB directory exists (Railway volumes mount at /data)
os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)

# ===================================================
# FINANCIALJUICE NEWS FEED
# ===================================================
FJ_RSS_URL = "https://www.financialjuice.com/feed.ashx?xy=rss"
FJ_POLL_INTERVAL = 30
FJ_MAX_HEADLINES = 20
ENABLE_NEWS = os.environ.get("ENABLE_NEWS", "true").lower() == "true"
CHART_URL   = os.environ.get("CHART_URL", "https://www.tradingview.com/chart/jDqlGthU/")

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
            check_news_impact()
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


# NOTE: start_news_thread() is called from gunicorn post_fork hook (gunicorn.conf.py)
# This prevents duplicate pollers from master + worker processes


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
# AUTONOMOUS NEWS IMPACT ALERTS
# ===================================================
HIGH_IMPACT_KEYWORDS = [
    # ── Fed / Monetary Policy ──
    "fomc", "fed rate", "rate decision", "rate cut", "rate hike", "powell",
    "federal reserve", "quantitative", "tapering", "hawkish", "dovish",
    "fed funds", "monetary policy", "interest rate", "basis points",
    "fed minutes", "fed meeting", "fed pivot", "rate hold", "rate pause",
    "waller", "williams", "bostic", "barkin", "kashkari", "goolsbee",
    "mester", "daly", "logan", "bowman", "jefferson", "cook",
    "balance sheet", "reverse repo", "rrp", "quantitative tightening",
    # ── Economic Data Releases ──
    "nfp", "non-farm", "payroll", "cpi", "inflation", "ppi", "gdp",
    "jobless claims", "unemployment", "retail sales", "ism",
    "pce", "core pce", "consumer confidence", "consumer sentiment",
    "michigan sentiment", "durable goods", "housing starts",
    "building permits", "existing home", "new home sales", "pending home",
    "industrial production", "capacity utilization", "jolts",
    "adp employment", "initial claims", "continuing claims",
    "import price", "export price", "trade balance", "trade deficit",
    "current account", "productivity", "unit labor cost",
    "empire state", "philly fed", "chicago pmi", "dallas fed",
    "richmond fed", "kansas city fed", "beige book",
    # ── Treasury / Bonds / Yields ──
    "treasury", "10-year", "10 year", "2-year", "2 year", "30-year",
    "yield", "bond auction", "bid-to-cover", "inversion", "yield curve",
    "treasury auction", "bond sell", "bond rally", "sovereign debt",
    "municipal bond", "corporate bond", "junk bond", "high yield",
    "credit spread", "swap spread",
    # ── Geopolitical / Trade War ──
    "tariff", "trade war", "sanction", "invasion", "war ", "missile",
    "nato", "china retaliate", "escalat", "nuclear", "ceasefire",
    "embargo", "blockade", "military", "troops", "strike ",
    "retaliat", "counter-tariff", "countermeasure", "trade deal",
    "trade agreement", "trade tension", "export control", "chip ban",
    "huawei", "semiconductor ban", "rare earth", "supply chain",
    # ── Trump / US Politics ──
    "trump", "executive order", "truth social", "government shutdown",
    "debt ceiling", "debt limit", "congress", "white house",
    "impeach", "indictment", "election", "biden", "republican",
    "democrat", "legislation", "fiscal policy", "spending bill",
    "continuing resolution",
    # ── China / Asia ──
    "china", "beijing", "xi jinping", "pboc", "yuan", "devalue",
    "renminbi", "china gdp", "china pmi", "caixin", "shanghai",
    "hang seng", "nikkei", "boj", "bank of japan", "yen",
    "south china sea", "taiwan", "chips act",
    # ── Oil / Energy / Commodities ──
    "crude oil", "wti", "brent", "opec", "oil price", "oil surge",
    "oil crash", "energy crisis", "natural gas", "gasoline",
    "petroleum", "oil inventory", "eia", "drilling rig",
    "oil production", "oil cut", "opec+", "saudi", "gold surge",
    "gold crash", "copper", "commodity",
    # ── Currencies / Dollar ──
    "dollar index", "dxy", "euro", "eur/usd", "gbp", "sterling",
    "forex", "currency", "fx ", "dollar surge", "dollar crash",
    "strong dollar", "weak dollar", "dollar selloff",
    # ── Market Events / Crashes ──
    "circuit breaker", "halt", "flash crash", "margin call", "liquidat",
    "bank failure", "default", "downgrade", "credit rating",
    "black swan", "volatility spike", "vix spike", "vix surge",
    "sell-off", "selloff", "capitulat", "panic", "crash",
    "correction", "bear market", "recession", "stagflation",
    "bank run", "contagion", "systemic risk", "too big to fail",
    # ── Earnings / Big Tech (ES movers) ──
    "earnings", "earnings miss", "earnings beat", "revenue miss",
    "guidance cut", "guidance raise", "profit warning",
    "nvidia", "apple", "microsoft", "amazon", "google", "alphabet",
    "meta", "tesla", "magnificent seven", "mag 7", "big tech",
    "ai chip", "semiconductor", "chip stock",
    # ── Central Banks (non-Fed) ──
    "ecb", "european central bank", "lagarde", "bank of england",
    "boe", "rba", "reserve bank", "snb", "swiss national",
    # ── ES / Equity Index Specific ──
    "s&p 500", "s&p500", "es futures", "spx", "equity futures",
    "nasdaq", "dow jones", "russell", "futures surge", "futures drop",
    "futures plunge", "pre-market", "after-hours",
    "market open", "market close", "triple witch", "quad witch",
    "opex", "options expir", "gamma", "0dte",
    # ── Crypto Spillover (risk sentiment) ──
    "bitcoin crash", "crypto crash", "tether", "stablecoin",
    "bitcoin surge", "crypto rally",
    # ── Natural Disasters (USGS enabled) ──
    "earthquake", "tsunami", "hurricane", "typhoon", "wildfire",
]

seen_headlines = set()
seen_headlines_lock = threading.Lock()
NEWS_COOLDOWN_MINUTES = 5  # Min time between news alerts
_last_news_alert_time = 0  # In-memory cooldown (instant, no DB delay)
_news_alert_lock = threading.Lock()  # Prevent concurrent news processing


def get_headline_key(headline):
    """Normalize headline for fuzzy dedup — first 50 chars lowercase, strip punctuation"""
    import string
    clean = headline.lower().translate(str.maketrans('', '', string.punctuation))
    return clean[:50].strip()


def was_recently_alerted(headline_key):
    """Check if we already alerted on a similar headline recently (DB-backed, survives restarts)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        cutoff = (datetime.utcnow() - timedelta(minutes=NEWS_COOLDOWN_MINUTES * 3)).isoformat()
        c.execute("""
            SELECT verdict FROM alerts
            WHERE alert_type = 'NEWS_IMPACT' AND timestamp > ?
            ORDER BY id DESC LIMIT 10
        """, (cutoff,))
        recent_news = c.fetchall()
        conn.close()

        for row in recent_news:
            stored_key = get_headline_key(row[0]) if row[0] else ""
            # If first 30 chars match = same story updated
            if stored_key[:30] == headline_key[:30] and stored_key[:30] != "":
                return True
        return False
    except Exception:
        return False


def news_on_cooldown():
    """Check if any news alert was sent within cooldown period"""
    global _last_news_alert_time
    # In-memory check first (instant, catches the gap while Claude API runs)
    if time.time() - _last_news_alert_time < NEWS_COOLDOWN_MINUTES * 60:
        return True
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        cutoff = (datetime.utcnow() - timedelta(minutes=NEWS_COOLDOWN_MINUTES)).isoformat()
        c.execute("SELECT COUNT(*) FROM alerts WHERE alert_type = 'NEWS_IMPACT' AND timestamp > ?", (cutoff,))
        count = c.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


def get_active_trade():
    """Query DB for most recent active trade (entry without matching exit)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Get last entry/re-entry/reversal
        c.execute("""
            SELECT alert_type, direction, price, timestamp, tl_state, rsi, adx, open_pnl
            FROM alerts
            WHERE alert_type IN ('ENTRY', 'RE_ENTRY', 'REVERSAL')
            ORDER BY id DESC LIMIT 1
        """)
        last_entry = c.fetchone()

        if not last_entry:
            conn.close()
            return None

        # Check if there's a more recent exit
        c.execute("""
            SELECT alert_type FROM alerts
            WHERE alert_type IN ('SESSION_CLOSE', 'FRIDAY_CLOSE', 'REVERSAL', 'BANK_EXIT', 'REVERSAL_EXIT')
            AND id > (SELECT MAX(id) FROM alerts WHERE alert_type IN ('ENTRY', 'RE_ENTRY'))
            ORDER BY id DESC LIMIT 1
        """)
        last_exit = c.fetchone()
        conn.close()

        # If last exit is after last entry, no active trade
        # (REVERSAL counts as both exit + entry, so it's still "active")
        if last_exit and last_entry[0] != 'REVERSAL':
            return None

        return {
            "direction": last_entry[1],
            "price": last_entry[2],
            "timestamp": last_entry[3],
            "tl_state": last_entry[4],
            "rsi": last_entry[5],
            "adx": last_entry[6],
            "open_pnl": last_entry[7],
        }
    except Exception as e:
        print(f"NEWS: Active trade query error: {e}")
        return None


def is_high_impact(headline):
    """Check if headline contains high-impact keywords"""
    lower = headline.lower()
    for kw in HIGH_IMPACT_KEYWORDS:
        if kw in lower:
            return True
    return False


def analyze_news_impact(headline, active_trade):
    """Breaking news + active trade → assess risk to position"""
    if not ANTHROPIC_API_KEY or not ENABLE_CLAUDE:
        return "", ""

    prompt = f"""{SYSTEM_KNOWLEDGE}

BREAKING NEWS DETECTED:
  Headline: {headline}

ACTIVE TRADE:
  Direction: {active_trade['direction']}
  Entry Price: {active_trade['price']}
  TL State: {active_trade['tl_state']}
  RSI at entry: {active_trade['rsi']}
  ADX at entry: {active_trade['adx']}

Assess this news headline's impact on the active {active_trade['direction']} ES position.
Be specific: Is this bullish, bearish, or neutral for ES?
If it SUPPORTS the trade → say "supports position, hold through"
If it THREATENS the trade → say "threatens position, consider taking profit" or "tighten mental stop"
If it creates volatility → say "expect chop, widen awareness"
Keep it to 2-3 sentences. End with one of:
[HIGH RISK] — directly threatens position, consider manual exit or take profit
[MEDIUM RISK] — creates uncertainty, heighten awareness
[LOW RISK] — unlikely to impact or supports current trade"""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if response.status_code == 200:
            text = response.json()["content"][0]["text"]
            risk = "MEDIUM"
            if "[HIGH RISK]" in text: risk = "HIGH"
            elif "[LOW RISK]" in text: risk = "LOW"
            clean = text.replace("[HIGH RISK]", "").replace("[MEDIUM RISK]", "").replace("[LOW RISK]", "").strip()
            return clean, risk
    except Exception as e:
        print(f"NEWS: Claude impact error: {e}")
    return "", ""


def analyze_news_opportunity(headline):
    """Breaking news while FLAT → assess entry opportunity"""
    if not ANTHROPIC_API_KEY or not ENABLE_CLAUDE:
        return "", ""

    prompt = f"""{SYSTEM_KNOWLEDGE}

BREAKING NEWS DETECTED (currently FLAT — no open ES position):
  Headline: {headline}

Assess this news headline's impact on ES futures for someone with NO position:
1. Is this bullish, bearish, or neutral for ES?
2. Does this create a potential entry opportunity? If so, which direction?
   e.g. "Bearish headline may create oversold dip — watch for LONG entry signal"
   e.g. "Rate hike surprise — expect sustained selling, watch for SHORT entry"
3. Key timing: Is the move likely immediate or will it develop over hours?
4. Volatility warning if applicable
Keep it to 2-3 sentences. End with one of:
[OPPORTUNITY] — likely creates a tradeable move, watch for system entry signal
[WATCH] — creates uncertainty, be ready for signals in either direction
[NO ACTION] — unlikely to move ES meaningfully"""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if response.status_code == 200:
            text = response.json()["content"][0]["text"]
            level = "WATCH"
            if "[OPPORTUNITY]" in text: level = "OPPORTUNITY"
            elif "[NO ACTION]" in text: level = "NO ACTION"
            clean = text.replace("[OPPORTUNITY]", "").replace("[WATCH]", "").replace("[NO ACTION]", "").strip()
            return clean, level
    except Exception as e:
        print(f"NEWS: Claude opportunity error: {e}")
    return "", ""


def send_news_alert(headline, active_trade, analysis, risk_level):
    """Send news alert to Discord — works for both in-trade and flat"""
    if not DISCORD_WEBHOOK_URL:
        return

    risk_config = {
        "HIGH":        {"emoji": "🚨", "color": 15548997, "label": "HIGH RISK"},
        "MEDIUM":      {"emoji": "⚠️", "color": 16750848, "label": "MEDIUM RISK"},
        "LOW":         {"emoji": "ℹ️", "color": 3447003,  "label": "LOW RISK"},
        "OPPORTUNITY": {"emoji": "🔔", "color": 5763719,  "label": "OPPORTUNITY"},
        "WATCH":       {"emoji": "👀", "color": 16750848, "label": "WATCH"},
        "NO ACTION":   {"emoji": "ℹ️", "color": 9807270,  "label": "NO ACTION"},
    }
    cfg = risk_config.get(risk_level, risk_config["MEDIUM"])

    lines = []
    lines.append(f"📰 **NEWS ALERT**")
    lines.append("")
    lines.append(f"**{headline}**")
    lines.append("")

    if active_trade:
        dir_emoji = "🟢" if active_trade["direction"] == "LONG" else "🔴"
        lines.append(f"Position: {dir_emoji} **{active_trade['direction']}** @ ${active_trade['price']:,.2f}")
    else:
        lines.append("Position: **FLAT** — no open trade")

    lines.append("")
    lines.append(f"{cfg['emoji']} **{cfg['label']}**")
    lines.append("────────────────────")
    lines.append(analysis)

    embed = {
        "description": "\n".join(lines),
        "color": cfg["color"],
        "footer": {"text": "⚡ OneMP11 News Monitor"},
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    }

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed], "username": "OneMP11"}, timeout=10)
        print(f"NEWS: Sent {risk_level} alert for: {headline[:60]}")
    except Exception as e:
        print(f"NEWS: Discord error: {e}")

    store_alert({
        "alert_type": "NEWS_IMPACT",
        "direction": active_trade["direction"] if active_trade else "FLAT",
        "price": active_trade["price"] if active_trade else 0,
        "verdict": headline[:200],
        "claude_analysis": analysis,
        "claude_confidence": risk_level,
        "timestamp": datetime.utcnow().isoformat(),
    })


def check_news_impact():
    """Check new headlines for high-impact events, alert whether in trade or not.
    Uses threading lock + DB claim to prevent duplicate alerts across workers/threads."""

    # Lock prevents concurrent processing within the same worker
    if not _news_alert_lock.acquire(blocking=False):
        return  # Another thread is already processing news
    try:
        _check_news_impact_locked()
    finally:
        _news_alert_lock.release()


def _check_news_impact_locked():
    """Inner function — always called under _news_alert_lock"""
    with news_cache_lock:
        items = list(news_cache)

    if not items:
        return

    # Cooldown: max 1 news alert per N minutes
    if news_on_cooldown():
        return

    # Cleanup old dedup entries (> 1 hour) to prevent table bloat
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        c = conn.cursor()
        cutoff = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        c.execute("DELETE FROM news_sent WHERE timestamp < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass

    active_trade = get_active_trade()

    for item in items[:5]:
        title = item.get("title", "")
        if not title:
            continue

        if not is_high_impact(title):
            continue

        headline_key = get_headline_key(title)

        # ATOMIC DEDUP — INSERT OR IGNORE with UNIQUE constraint
        # If another worker/thread already claimed this headline, rowcount = 0
        # This is the ONLY dedup check needed — no race conditions possible
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            c = conn.cursor()
            c.execute("""
                INSERT OR IGNORE INTO news_sent (headline_key, headline, timestamp)
                VALUES (?, ?, ?)
            """, (headline_key, title[:200], datetime.utcnow().isoformat()))
            claimed = c.rowcount > 0  # 1 = we claimed it, 0 = already exists
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"NEWS: Dedup DB error: {e}")
            continue

        if not claimed:
            print(f"NEWS: Already claimed: {title[:50]}")
            continue

        print(f"NEWS: High-impact detected: {title[:60]}")

        # Set cooldown IMMEDIATELY — before Claude API call (15-30 sec)
        global _last_news_alert_time
        _last_news_alert_time = time.time()

        if active_trade:
            analysis, risk = analyze_news_impact(title, active_trade)
        else:
            analysis, risk = analyze_news_opportunity(title)

        if analysis:
            send_news_alert(title, active_trade, analysis, risk)
        break  # One alert per poll cycle


# ===================================================
# V8.1b KNOWLEDGE BASE (updated — no SL/Smart)
# ===================================================
SYSTEM_KNOWLEDGE = """
You are the OneMP11 V8.1b trading system analyst for ES futures.
Your job: assess each alert using the system's rules, database history, and market context.

SIGNAL GENERATION:
- Entries require ALL four: CVD Momentum > 60, Histogram > 5, Price above/below Kalman VWAP, Kalman slope confirms direction
- ADX > 20 required for entries and re-entries only
- Reversals fire when ALL four conditions flip — ADX does NOT gate reversals
- Re-entries on momentum flip back to trend (max 3 per trend)
- Exit mode: HOLD UNTIL REVERSAL (no SL, no smart exit/stop — data proved they hurt)

TL SPREAD (ATR-based):
- TIGHT (< 0.5x ATR): Price hugging TL — strong conviction
- RIDING (0.5-1.0x ATR): Normal trend following
- EXTENDED (1.0-2.0x ATR): Getting stretched
- STRETCHED (> 2.0x ATR): Overextended — high reversion risk

SESSION FILTER:
- No-entry zone: 2pm-5pm CT (blocks entries + re-entries, NOT reversals)
- Reversals ALWAYS fire regardless of no-entry zone (V8.1c fix)
- Force close: 4pm CT Mon-Thu (always ON)
- Friday auto-close: 4pm (market closed Fri 4pm - Sun 5pm)
- Session open: 5pm CT (ES futures reopen)

ALERT TYPES:
- ENTRY: Fresh long/short — all 4 conditions aligned (strongest signal)
- RE_ENTRY: Momentum flipped back to trend after pullback
- REVERSAL: All conditions flipped — exits current AND enters opposite
- BANK_EXIT: Traffic light = BANK IT while in profit → auto-close, keeps tradeDir alive for re-entry
- STOP_WARNING: Trade hit hard stop threshold — Claude analyzes and recommends CUT or HOLD
- CUT_WARNING: Traffic light all red + underwater — Claude analyzes and recommends CUT or HOLD
- BANK_WARNING: Traffic light all red + in profit — Claude analyzes and recommends BANK or HOLD
- SESSION_CLOSE / FRIDAY_CLOSE: Force exit at 4pm CT
- MILESTONE_UP: Trade hit +10, +20, or +30 pts profit
- MILESTONE_DOWN: Trade hit -15 or -25 pts loss
- MARKET_CHECK: 10am CST daily snapshot
- REGIME_SHIFT: VIX regime changed
- NO_ENTRY: 2pm block started
- SESSION_OPEN: 5pm session open
- NEWS_IMPACT: High-impact news detected while trade is active

HOW TO ANALYZE EACH FIELD:

CVD Momentum (-100 to +100):
- Above +60 = strong buying flow (entry quality)
- 0 to +60 = weak buying (fading or building)
- Below -60 = strong selling flow
- CRITICAL: If CVD momentum direction DISAGREES with trade direction, flag it

Candle Color (from CVD):
- STRONG_BULL / STRONG_BEAR = entry-quality flow, high conviction
- WEAK_BULL / WEAK_BEAR = flow exists but not strong
- NEUTRAL = dead flow, no directional edge
- DIVERGENCE ALERT: If trade is LONG but candle is WEAK_BEAR or worse → flag as "flow diverging"
- DIVERGENCE ALERT: If candle was STRONG_BULL and shifted to WEAK_BULL → flag as "flow fading"

Traffic Light (hold/bank signal from data analysis):
- GGG = all green → HOLD with confidence (mention this explicitly)
- GGR / GRG = lean hold, one concern → note which check failed
- RRG / RGR / GRR = caution → recommend tightening mentally
- RRR = all red → strongly suggest banking profit if in the money
- ALWAYS mention the traffic light reading in your analysis

Kalman Slope:
- Positive = TL rising (bullish bias)
- Negative = TL falling (bearish bias)
- Magnitude > 0.3 = steep/fast trend (may exhaust)
- Magnitude < 0.05 = flat/indecisive

Spread Ratio (ATR-normalized):
- < 0.5 = TIGHT (strong conviction zone)
- 0.5-1.0 = RIDING (healthy trend)
- 1.0-2.0 = EXTENDED (stretched)
- > 2.0 = STRETCHED (high reversion risk)
- If spread ratio > 1.5 AND RSI extreme → giveback probability HIGH

RSI Cross-Market Context:
- ES RSI 55-65 = sweet spot (only 9% giveback historically)
- ES RSI < 45 or > 70 = danger zone (40% giveback)
- VIX RSI > 60 = fear rising (bearish pressure)
- VIX RSI < 30 = complacency (bullish support)
- CL RSI diverging from ES RSI = cross-market stress

ADR TARGET LEVELS (PivotBoss ADR method):
- ADR = 10-day Average Daily Range. Levels calculated from today's low (bull) or high (bear).
- R75% / S75% = Primary target (75% of average daily move used). Good first bank zone.
- R100% / S100% = Full ADR exhausted. Only ~5% of days exceed this. STRONG bank signal.
- R125% / S125% = Extended day. Rare — lock in profits unless extreme trend day (ADX 40+, GGG traffic).
- When ADR_R100 or ADR_S100 fires:
  → If traffic is RRR/RGR/YRR → "BANK IT — full ADR + momentum dying"
  → If traffic is GGG + ADX 35+ → "HOLD — trend day, could push to 125%"
  → If traffic is mixed → "BANK HALF — protect gains but leave runner"
- ADR% in [D] data line shows how much of daily range is consumed (e.g. ADR:95 = 95% used)
- TGT in [D] data line shows the next ADR target price level

NEWS IMPACT RULES:
- HIGH IMPACT events (Fed decisions, CPI, NFP, tariffs, geopolitical escalation):
  → If headline clearly affects ES direction AND you have an active trade → flag risk level
  → Tariffs/trade war → bearish ES pressure
  → Fed dovish/rate cuts → bullish ES
  → Surprise economic miss → volatile, direction depends on context
- MEDIUM IMPACT (earnings, sector news, oil spikes):
  → Note but don't change confidence unless directly relevant to ES
- LOW IMPACT (general commentary, analyst opinions):
  → Ignore for trading purposes
- NEVER override the system's signal based on news alone
- DO mention when news creates heightened volatility risk

CONFIDENCE RULES:
- HIGH: Strong signal alignment + database supports + traffic green + no conflicting news
- MEDIUM: Mostly aligned but one concern (extended TL, fading momentum, session risk, cautious traffic light)
- LOW: Multiple concerns (low ADX, STRETCHED TL, RSI extreme, flow diverging, traffic red, adverse news)
- ADX value of -1 means "not included in this alert type" — do NOT treat as zero/weak
- ALWAYS reference specific database stats (e.g. "Your LONG trades from RIDING have won 72%")
"""


# ===================================================
# DATABASE
# ===================================================
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, alert_type TEXT, direction TEXT, price REAL,
                exit_pts REAL, daily_pnl REAL, weekly_pnl REAL, monthly_pnl REAL,
                total_pnl REAL, tl_spread REAL, tl_state TEXT, rsi REAL,
                vix_rsi REAL, compare_rsi REAL, adx REAL, verdict TEXT,
                claude_analysis TEXT, claude_confidence TEXT, raw_json TEXT, session TEXT,
                cvd_mom REAL DEFAULT 0, histogram REAL DEFAULT 0,
                kalman_slope REAL DEFAULT 0, candle_color TEXT DEFAULT '',
                traffic TEXT DEFAULT '', regime TEXT DEFAULT '',
                spread_ratio REAL DEFAULT 0, open_pnl REAL DEFAULT 0,
                source TEXT DEFAULT 'V8_1B'
            )
        """)
        # Migration for existing DBs — add new columns if missing
        new_cols = [
            ("session", "TEXT DEFAULT ''"), ("cvd_mom", "REAL DEFAULT 0"),
            ("histogram", "REAL DEFAULT 0"), ("kalman_slope", "REAL DEFAULT 0"),
            ("candle_color", "TEXT DEFAULT ''"), ("traffic", "TEXT DEFAULT ''"),
            ("regime", "TEXT DEFAULT ''"), ("spread_ratio", "REAL DEFAULT 0"),
            ("open_pnl", "REAL DEFAULT 0"),
            ("source", "TEXT DEFAULT 'V8_1B'"),
        ]
        for col, ctype in new_cols:
            try:
                c.execute(f"ALTER TABLE alerts ADD COLUMN {col} {ctype}")
            except sqlite3.OperationalError:
                pass
        conn.commit()

        # Atomic news dedup table — UNIQUE constraint prevents duplicate alerts
        conn2 = sqlite3.connect(DB_PATH)
        c2 = conn2.cursor()
        c2.execute("""
            CREATE TABLE IF NOT EXISTS news_sent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                headline_key TEXT UNIQUE,
                headline TEXT,
                timestamp TEXT
            )
        """)
        conn2.commit()
        conn2.close()

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        conn.close()
        print(f"DB: Initialized at {DB_PATH}")

        # Verify write works
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM alerts")
        count = c.fetchone()[0]
        conn.close()
        print(f"DB: {count} existing alerts")
    except Exception as e:
        print(f"DB INIT ERROR: {e}")
        traceback.print_exc()


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
    atype = data.get("alert_type", "UNKNOWN")
    source = data.get("source", "V8_1B")
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        c = conn.cursor()
        c.execute("""
            INSERT INTO alerts (
                timestamp, alert_type, direction, price, exit_pts,
                daily_pnl, weekly_pnl, monthly_pnl, total_pnl,
                tl_spread, tl_state, rsi, vix_rsi, compare_rsi,
                adx, verdict, claude_analysis, claude_confidence, raw_json, session,
                cvd_mom, histogram, kalman_slope, candle_color, traffic, regime,
                spread_ratio, open_pnl, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            data.get("raw_json", ""), session,
            data.get("cvd_mom", 0), data.get("histogram", 0),
            data.get("kalman_slope", 0), data.get("candle_color", ""),
            data.get("traffic", ""), data.get("regime", ""),
            data.get("spread_ratio", 0), data.get("open_pnl", 0),
            data.get("source", "V8_1B")
        ))
        conn.commit()
        new_id = c.lastrowid
        conn.close()
        print(f"DB: Stored {source}/{atype} as id={new_id}")
        return True
    except Exception as e:
        print(f"DB ERROR storing {source}/{atype}: {e}")
        traceback.print_exc()
        return False


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
# MULTI-SOURCE DETECTION
# ===================================================
def detect_source(raw_json):
    """Detect which script sent the alert"""
    if isinstance(raw_json, dict):
        # SPY/VIX script sends embeds with title pattern "🟢 ES1! — BULLISH"
        embeds = raw_json.get("embeds", [])
        if embeds and isinstance(embeds, list) and len(embeds) > 0:
            title = embeds[0].get("title", "")
            if "BULLISH" in title or "BEARISH" in title or "TP HIT" in title:
                return "SPY_VIX"
        content = raw_json.get("content", "")
        if "[RSI-PROFILE]" in content:
            return "RSI_PROFILE"
    return "V8_1B"


def parse_spyvix_alert(raw_json):
    """Parse SPY/VIX Discord TradeBot alert (embeds format)"""
    data = {
        "alert_type": "", "direction": "", "price": 0, "exit_pts": 0,
        "rsi": 0, "vix_rsi": 0, "adx": -1, "verdict": "", "traffic": "",
        "source": "SPY_VIX", "raw_json": json.dumps(raw_json),
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        embeds = raw_json.get("embeds", [{}])
        embed = embeds[0] if embeds else {}
        title = embed.get("title", "")

        # TP Hit alert
        if "TP HIT" in title:
            data["alert_type"] = "SPY_VIX_TP"
            data["direction"] = "LONG" if "LONG" in title else "SHORT"
            for field in embed.get("fields", []):
                val = field.get("value", "")
                if "pts" in val:
                    m = re.search(r'([+-]?\d+\.?\d*).*pts', val)
                    if m:
                        data["exit_pts"] = float(m.group(1))
            return data

        # Entry signal
        if "BULLISH" in title:
            data["alert_type"] = "SPY_VIX_ENTRY"
            data["direction"] = "LONG"
        elif "BEARISH" in title:
            data["alert_type"] = "SPY_VIX_ENTRY"
            data["direction"] = "SHORT"

        # Extract confidence from title "🟢 ES1! — BULLISH (72%)"
        conf_match = re.search(r'\((\d+)%\)', title)
        if conf_match:
            data["verdict"] = f"SPY/VIX Confidence: {conf_match.group(1)}%"

        # Parse fields
        for field in embed.get("fields", []):
            name = field.get("name", "")
            val = field.get("value", "")

            if "Technical" in name:
                rsi_m = re.search(r'SPY\s*(\d+)', val)
                if rsi_m:
                    data["rsi"] = float(rsi_m.group(1))
                vix_m = re.search(r'VIX\s*(\d+)', val)
                if vix_m:
                    data["vix_rsi"] = float(vix_m.group(1))

            elif "Levels" in name:
                entry_m = re.search(r'Entry.*?\$(\d[\d,.]+)', val)
                if entry_m:
                    data["price"] = float(entry_m.group(1).replace(",", ""))

            elif "HTF" in name:
                htf_m = re.search(r'Alignment.*?(\d+)%', val)
                if htf_m:
                    data["traffic"] = f"HTF:{htf_m.group(1)}%"

    except Exception as e:
        data["alert_type"] = "SPY_VIX_PARSE_ERROR"
        data["verdict"] = str(e)
    return data


def parse_rsi_profile_alert(raw_json):
    """Parse RSI Profile Overlay alert"""
    data = {
        "alert_type": "", "direction": "", "price": 0,
        "rsi": 0, "adx": -1, "verdict": "", "source": "RSI_PROFILE",
        "raw_json": json.dumps(raw_json) if isinstance(raw_json, dict) else str(raw_json),
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        content = raw_json.get("content", "") if isinstance(raw_json, dict) else ""

        if "TIER2 LONG" in content or "ZONE LONG" in content:
            data["alert_type"] = "RSI_PROFILE_LONG"
            data["direction"] = "LONG"
        elif "TIER2 SHORT" in content or "ZONE SHORT" in content:
            data["alert_type"] = "RSI_PROFILE_SHORT"
            data["direction"] = "SHORT"
        elif "TIER1 LONG" in content:
            data["alert_type"] = "RSI_PROFILE_PENDING_LONG"
            data["direction"] = "LONG"
        elif "TIER1 SHORT" in content:
            data["alert_type"] = "RSI_PROFILE_PENDING_SHORT"
            data["direction"] = "SHORT"

        # Parse data fields: RSI:58 POC:52 ADX:27 VWAP:6816.50
        for pattern, key in [(r'RSI:(\d+)', 'rsi'), (r'ADX:(\d+)', 'adx'),
                              (r'VWAP:\$?([\d,.]+)', 'price')]:
            m = re.search(pattern, content)
            if m:
                data[key] = float(m.group(1).replace(",", ""))

        # Zone range
        zone_m = re.search(r'Zone:\s*\$([\d,.]+)-\$([\d,.]+)', content)
        if zone_m:
            data["verdict"] = f"Zone: ${zone_m.group(1)}-${zone_m.group(2)}"

    except Exception as e:
        data["alert_type"] = "RSI_PROFILE_PARSE_ERROR"
        data["verdict"] = str(e)
    return data


def get_confluence_context(alert_data, minutes=15):
    """Query DB for recent signals from OTHER sources for confluence analysis"""
    source = alert_data.get("source", "V8_1B")
    direction = alert_data.get("direction", "")
    cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT source, alert_type, direction, price, rsi, verdict, timestamp
            FROM alerts
            WHERE source != ? AND timestamp > ?
            ORDER BY id DESC LIMIT 20
        """, (source, cutoff))
        rows = c.fetchall()
        conn.close()

        if not rows:
            return "\nCROSS-INDICATOR CONFLUENCE: No recent signals from other indicators."

        lines = ["\nCROSS-INDICATOR CONFLUENCE (last 15 min):"]
        agrees = 0
        disagrees = 0

        for row in rows:
            src, atype, dir, price, rsi, verdict, ts = row
            src_label = {"SPY_VIX": "SPY/VIX TradeBot", "RSI_PROFILE": "RSI Profile", "V8_1B": "V8.1b"}.get(src, src)

            # Check agreement
            if direction and dir:
                if dir == direction:
                    agrees += 1
                    emoji = "✅"
                else:
                    disagrees += 1
                    emoji = "❌"
            else:
                emoji = "ℹ️"

            time_str = ts[-8:-3] if len(ts) > 8 else ts
            lines.append(f"  {emoji} {src_label}: {atype} {dir} (RSI:{rsi:.0f}) @ {time_str}")
            if verdict:
                lines.append(f"     {verdict}")

        # Summary
        total = agrees + disagrees
        if total > 0:
            if agrees > 0 and disagrees == 0:
                lines.append(f"\n  🟢 ALL {agrees} indicator(s) AGREE with {direction} — STRONG confluence")
            elif agrees > disagrees:
                lines.append(f"\n  🟡 {agrees}/{total} agree, {disagrees} disagree — MODERATE confluence")
            elif disagrees > agrees:
                lines.append(f"\n  🔴 {disagrees}/{total} DISAGREE — WEAK confluence, consider skipping")
            else:
                lines.append(f"\n  🟡 Mixed signals — proceed with caution")

        return "\n".join(lines)

    except Exception as e:
        return f"\nCONFLUENCE: Error querying — {e}"


# ===================================================
# ALERT PARSER (synced with V8.1b — fixed price/ADX)
# ===================================================
def parse_alert(raw_json):
    data = {
        "alert_type": "", "direction": "", "price": 0, "exit_pts": 0,
        "daily_pnl": 0, "weekly_pnl": 0, "monthly_pnl": 0, "total_pnl": 0,
        "tl_spread": 0, "tl_state": "", "rsi": 0, "vix_rsi": 0,
        "compare_rsi": 0, "adx": -1, "verdict": "", "traffic": "",
        "cvd_mom": 0, "histogram": 0, "kalman_slope": 0, "candle_color": "",
        "regime": "", "spread_ratio": 0, "open_pnl": 0, "source": "V8_1B",
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
        elif "REVERSAL EXIT" in content:
            data["alert_type"] = "REVERSAL_EXIT"
            data["direction"] = "LONG" if "Exited LONG" in content else "SHORT"
        elif "REVERSAL" in content:
            data["alert_type"] = "REVERSAL"
            if "GO LONG" in content:
                data["direction"] = "LONG"
            elif "GO SHORT" in content:
                data["direction"] = "SHORT"
            else:
                data["direction"] = "LONG" if "Exited SHORT" in content else "SHORT"
        elif "HARD STOP WARNING" in content:
            data["alert_type"] = "STOP_WARNING"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif "CUT LOSS WARNING" in content:
            data["alert_type"] = "CUT_WARNING"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif "BANK IT WARNING" in content:
            data["alert_type"] = "BANK_WARNING"
            data["direction"] = "LONG" if "LONG" in content else "SHORT"
        elif "BANK EXIT" in content:
            data["alert_type"] = "BANK_EXIT"
            data["direction"] = "LONG" if "Exited LONG" in content else "SHORT"
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
        elif "ADR 125%" in content:
            data["alert_type"] = "ADR_R125" if "LONG" in content else "ADR_S125"
        elif "ADR 100%" in content:
            data["alert_type"] = "ADR_R100" if "LONG" in content else "ADR_S100"
        elif "ADR 75%" in content:
            data["alert_type"] = "ADR_R75" if "LONG" in content else "ADR_S75"
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

        # Traffic light — "🟢🟢🟢 HOLD" or "🟡🔴🟢 CAUTION" etc.
        traffic_match = re.search(r'([🟢🟡🔴]{3,})\s*(HOLD|LEAN HOLD|CAUTION|BANK IT)', content)
        if traffic_match:
            data["traffic"] = f"{traffic_match.group(1)} {traffic_match.group(2)}"

        # [D] Compact data line — parse all metrics
        # Format: [D] M:85|H:42|S:0.15|C:SB|T:GGR|R:NORMAL|SR:1.23|OP:+15.2
        data_match = re.search(r'\[D\]\s*(.+?)$', content, re.MULTILINE)
        if data_match:
            data_str = data_match.group(1)
            for pair in data_str.split("|"):
                pair = pair.strip()
                if pair.startswith("M:"):
                    try: data["cvd_mom"] = float(pair[2:])
                    except: pass
                elif pair.startswith("H:"):
                    try: data["histogram"] = float(pair[2:])
                    except: pass
                elif pair.startswith("S:"):
                    try: data["kalman_slope"] = float(pair[2:])
                    except: pass
                elif pair.startswith("C:"):
                    color_map = {"SB": "STRONG_BULL", "WB": "WEAK_BULL", "SR": "STRONG_BEAR", "WR": "WEAK_BEAR", "N": "NEUTRAL"}
                    data["candle_color"] = color_map.get(pair[2:].strip(), pair[2:].strip())
                elif pair.startswith("T:"):
                    data["traffic"] = pair[2:].strip() if not data.get("traffic") else data["traffic"]
                elif pair.startswith("R:"):
                    data["regime"] = pair[2:].strip()
                elif pair.startswith("SR:"):
                    try: data["spread_ratio"] = float(pair[3:])
                    except: pass
                elif pair.startswith("OP:"):
                    try: data["open_pnl"] = float(pair[3:])
                    except: pass

    except Exception as e:
        data["alert_type"] = "PARSE_ERROR"
        data["verdict"] = str(e)

    return data


# ===================================================
# CHART IMAGE FETCHER (for Claude Vision)
# ===================================================
chart_image_cache = {"data": None, "timestamp": 0}
CHART_IMAGE_CACHE_SECONDS = 60  # Re-fetch chart image max every 60 seconds


def fetch_chart_image():
    """Fetch chart preview image from TradingView published chart"""
    if not CHART_URL:
        return None

    # Check cache
    now = time.time()
    if chart_image_cache["data"] and (now - chart_image_cache["timestamp"]) < CHART_IMAGE_CACHE_SECONDS:
        return chart_image_cache["data"]

    try:
        # Step 1: Fetch the chart page HTML to find og:image
        headers = {"User-Agent": "Mozilla/5.0 (compatible; OneMP11Bot/1.0)"}
        page = requests.get(CHART_URL, headers=headers, timeout=10)
        if page.status_code != 200:
            print(f"CHART: Page fetch failed: {page.status_code}")
            return None

        # Step 2: Extract og:image URL from meta tags
        import re as re_mod
        og_match = re_mod.search(r'<meta\s+property=["\']og:image["\']\s+content=["\'](https?://[^"\']+)["\']', page.text)
        if not og_match:
            # Try alternative pattern
            og_match = re_mod.search(r'content=["\'](https?://[^"\']+)["\'].*?property=["\']og:image["\']', page.text)
        if not og_match:
            print("CHART: No og:image found in page")
            return None

        image_url = og_match.group(1)
        print(f"CHART: Found image URL: {image_url[:80]}")

        # Step 3: Download the image
        img_response = requests.get(image_url, headers=headers, timeout=10)
        if img_response.status_code != 200:
            print(f"CHART: Image download failed: {img_response.status_code}")
            return None

        # Step 4: Base64 encode
        import base64
        img_b64 = base64.b64encode(img_response.content).decode("utf-8")

        # Detect media type
        content_type = img_response.headers.get("Content-Type", "image/png")
        if "jpeg" in content_type or "jpg" in content_type:
            media_type = "image/jpeg"
        else:
            media_type = "image/png"

        result = {"base64": img_b64, "media_type": media_type}
        chart_image_cache["data"] = result
        chart_image_cache["timestamp"] = now
        print(f"CHART: Image cached ({len(img_b64) // 1024}KB)")
        return result

    except Exception as e:
        print(f"CHART: Error fetching: {e}")
        return None


# ===================================================
# CLAUDE ANALYSIS (with optional Vision)
# ===================================================
def analyze_with_claude(alert_data, recent_alerts):
    if not ANTHROPIC_API_KEY or not ENABLE_CLAUDE:
        return "", ""

    pattern_context = get_pattern_analysis()
    similar = get_similar_trades(alert_data)
    news_context = get_news_context(max_items=5)
    confluence_context = get_confluence_context(alert_data, minutes=15)

    recent_context = ""
    if recent_alerts:
        recent_context = "\nRecent alerts:\n"
        for a in recent_alerts[:10]:
            pts_tag = f" exit:{a.get('exit_pts', 0):+.1f}pts" if a.get('exit_pts', 0) != 0 else ""
            src_tag = f" [{a.get('source', 'V8_1B')}]" if a.get('source', 'V8_1B') != 'V8_1B' else ""
            recent_context += f"  {a.get('alert_type', '')} {a.get('direction', '')}{pts_tag}{src_tag} ({a.get('session', '')})\n"

    similar_context = ""
    if similar["direction_trades"] > 0:
        similar_context = f"\nSIMILAR PATTERNS:\n  {similar['direction_trades']} {alert_data.get('direction', '')} trades: {similar['direction_wr']}% WR\n  {similar['tl_trades']} {similar['tl_state']} entries: {similar['tl_wr']}% WR\n  {similar['session_trades']} {similar['session']} trades: {similar['session_wr']}% WR"

    adx_val = alert_data.get('adx', -1)
    adx_str = "N/A (not in this alert type)" if adx_val == -1 else str(adx_val)
    source = alert_data.get('source', 'V8_1B')

    prompt = f"""{SYSTEM_KNOWLEDGE}

{pattern_context}
{similar_context}
{recent_context}
{confluence_context}
{news_context}

CURRENT ALERT (source: {source}):
  Type: {alert_data.get('alert_type', '')}
  Direction: {alert_data.get('direction', '')}
  Price: {alert_data.get('price', 0)}
  RSI: {alert_data.get('rsi', 0)}
  ADX: {adx_str}
  VIX RSI: {alert_data.get('vix_rsi', 0)} {"(off hours)" if alert_data.get('vix_rsi', 0) == 0 else ""}
  CL RSI: {alert_data.get('compare_rsi', 0)}
  TL: {alert_data.get('tl_spread', 0)} ({alert_data.get('tl_state', '')})
  Verdict: {alert_data.get('verdict', 'none')}
  Traffic Light: {alert_data.get('traffic', 'none')}
  CVD Momentum: {alert_data.get('cvd_mom', 0):.0f}
  Candle Color: {alert_data.get('candle_color', 'unknown')}
  Kalman Slope: {alert_data.get('kalman_slope', 0):.2f}
  Regime: {alert_data.get('regime', 'unknown')}
  Spread Ratio: {alert_data.get('spread_ratio', 0):.2f}x ATR
  Open P&L: {alert_data.get('open_pnl', 0):+.1f}pts
  Session: {get_session_from_time(alert_data.get('timestamp', ''))}

INSTRUCTIONS — Be actionable. The trader needs to make money, not read essays.

ALERT SOURCE MATTERS:
- V8.1b alerts (ENTRY, RE_ENTRY, REVERSAL, MILESTONE, SESSION_CLOSE, BANK_EXIT) = PRIMARY trading system
  → These are the actual trades. Give TAKE IT / SKIP / HOLD / BANK / CUT verdicts.
  → BANK_EXIT means traffic light went BANK IT while in profit. Trade closed, watching for re-entry.
  → For BANK_EXIT: confirm the exit was correct, note if re-entry conditions are building.
- STOP_WARNING / CUT_WARNING / BANK_WARNING = EXIT DECISION ALERTS
  → The trade is STILL OPEN. The system detected danger and is asking YOU to decide.
  → You MUST give a clear verdict: "CUT NOW" or "HOLD THROUGH" or "BANK NOW"
  → For CUT/HOLD decisions, analyze these factors:
    1. Is the trend structurally intact? (TL slope direction, ADX trending?)
    2. Is momentum building back or still fading? (CVD Mom direction, candle color)
    3. How extended is price from TL? (spread ratio — if TIGHT, pullback is normal)
    4. RSI position — is it at a reversal zone or mid-range?
    5. How did similar drawdowns resolve? (check DB: trades that hit -15 recovered X%)
    6. Time of day — is there enough session left for recovery?
    7. News context — any headlines threatening the position?
  → Be DECISIVE. The trader needs a clear answer, not a hedge.
  → If even ONE of these is strongly against → lean CUT
  → If trend/momentum are intact and it's just a pullback → HOLD THROUGH
- RSI_PROFILE alerts (TIER1, TIER2, ZONE) = SUPPORTING context
  → These are NOT separate trades. They tell you if RSI Profile agrees with V8.1b direction.
  → If V8.1b has an active LONG and RSI Profile fires LONG → "confluence confirms your trade"
  → If RSI Profile fires LONG but V8.1b hasn't entered → "RSI Profile sees opportunity, wait for V8.1b entry signal"
  → NEVER tell the trader to enter based on RSI Profile alone
- SPY_VIX alerts = SUPPORTING context
  → Same rules as RSI Profile — confirms or warns, doesn't override V8.1b
  → SPY/VIX confidence score is useful context for sizing, not for entry decisions

RESPONSE FORMAT:
1. Start with VERDICT: "TAKE IT" / "SKIP" / "HOLD" / "BANK PROFIT" / "CUT LOSS" / "CONTEXT NOTED"
   - Use "CONTEXT NOTED" for RSI_PROFILE and SPY_VIX alerts (they're not entries)
   - For CONTEXT NOTED: state whether it agrees/disagrees with current V8.1b position
2. One sentence explaining WHY (reference confluence + database stats)
3. For V8.1b entries: check DB for recent RSI_PROFILE and SPY_VIX signals — do they agree?
4. Keep it SHORT — 2-3 sentences max, no headers, no bullet lists
5. End with: [HIGH CONFIDENCE], [MEDIUM CONFIDENCE], or [LOW CONFIDENCE]

CONFLUENCE SCORING (only for V8.1b entry/reversal alerts):
- 3/3 indicators agree = HIGH confidence (mention "triple confluence")
- 2/3 agree = MEDIUM confidence (note the disagreeing one)
- 1/3 or 0/3 = LOW confidence (recommend skipping or reduced size)
- No other signals recently = judge on V8.1b signal alone using database history"""

    try:
        # Chart Vision temporarily disabled — TradingView og:image returns
        # generic placeholder for unlisted charts, not actual chart screenshot.
        # TODO: Re-enable when headless browser screenshots are available.
        chart_image = None

        # Build messages — with or without Vision
        if chart_image:
            messages = [{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": chart_image["media_type"],
                    "data": chart_image["base64"]
                }},
                {"type": "text", "text": prompt + """

A chart screenshot is attached. Here is what each visual element means:

CANDLE COLORS (CVD Flow — buying/selling pressure):
- Bright Green = STRONG BULL (CVD momentum > +60, heavy buying)
- Cyan/Light Blue = WEAK BULL (momentum 0 to +60, fading or building)
- Orange = WEAK BEAR (momentum 0 to -60, light selling)
- Pink/Red = STRONG BEAR (momentum < -60, heavy selling)
- Green→Cyan transition = momentum fading, watch for reversal
- Cyan→Orange = flow flipped bearish

LINES ON CHART:
- Green line (Kalman VWAP) = V8.1b's main trend line. Price above = bullish, below = bearish. Slope direction matters.
- Blue line (EMA 26) = Short-term moving average for trend reference
- RSI Trend Line Pro (changes color): Cyan = BULLISH slope, Purple = BEARISH slope, Yellow = NEUTRAL
- RSI TL Pro Upper Band (dashed above) = Overbought envelope
- RSI TL Pro Lower Band (dashed below) = Oversold envelope
- When RSI TL Pro turns from Cyan to Yellow = early warning trend weakening
- When RSI TL Pro turns from Yellow to Purple = confirmed bearish

KEY PATTERNS TO IDENTIFY:
- Candles green but RSI TL turning yellow/purple = DIVERGENCE (flow says buy but trend says weakening)
- Price above Kalman + RSI TL cyan + green candles = STRONG alignment, hold
- Price near RSI TL upper band = extended, giveback risk
- Candles shifting cyan→orange while Kalman still rising = early reversal warning

Describe what you see on the chart in 1 sentence, then give your verdict."""}
            ]}]
            print("CLAUDE: Using Vision (chart image attached)")
        else:
            messages = [{"role": "user", "content": prompt}]

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "messages": messages
            },
            timeout=45
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
        else:
            print(f"CLAUDE: API returned {response.status_code}: {response.text[:300]}")

    except Exception as e:
        print(f"Claude API error: {e}")
        traceback.print_exc()

    return "", ""


# ===================================================
# DISCORD — CLEAN TEXT FORMAT (matches TV alert style)
# ===================================================
ALERT_STYLES = {
    # V8.1b alerts
    "ENTRY":          {"emoji": "🟢", "color": 5763719,  "label": "ENTRY"},
    "RE_ENTRY":       {"emoji": "🔁", "color": 3447003,  "label": "RE-ENTRY"},
    "SESSION_CLOSE":  {"emoji": "⏸",  "color": 10070709, "label": "4PM CLOSE"},
    "FRIDAY_CLOSE":   {"emoji": "🔒", "color": 10070709, "label": "FRIDAY CLOSE"},
    "REVERSAL":       {"emoji": "🔄", "color": 15844367, "label": "REVERSAL"},
    "REVERSAL_EXIT":  {"emoji": "🔄", "color": 16750848, "label": "REVERSAL EXIT"},
    "TREND_OVER":     {"emoji": "❌", "color": 9807270,  "label": "TREND OVER"},
    "BANK_EXIT":      {"emoji": "💰", "color": 16766720, "label": "BANK EXIT"},
    "STOP_WARNING":   {"emoji": "🛑", "color": 15548997, "label": "HARD STOP WARNING"},
    "CUT_WARNING":    {"emoji": "✂️", "color": 15548997, "label": "CUT LOSS WARNING"},
    "BANK_WARNING":   {"emoji": "💰", "color": 16766720, "label": "BANK IT WARNING"},
    "MILESTONE_UP":   {"emoji": "📈", "color": 5763719,  "label": "MILESTONE UP"},
    "MILESTONE_DOWN": {"emoji": "📉", "color": 15548997, "label": "MILESTONE DOWN"},
    "MARKET_CHECK":   {"emoji": "📋", "color": 3447003,  "label": "10am MARKET CHECK"},
    "REGIME_SHIFT":   {"emoji": "🌡️", "color": 16750848, "label": "REGIME SHIFT"},
    "NO_ENTRY":       {"emoji": "⏸",  "color": 16750848, "label": "NO-ENTRY ZONE"},
    "SESSION_OPEN":   {"emoji": "🔔", "color": 3066993,  "label": "SESSION OPEN"},
    # RSI Profile alerts
    "RSI_PROFILE_LONG":           {"emoji": "🎯", "color": 5763719,  "label": "RSI PROFILE — LONG CONFIRMED"},
    "RSI_PROFILE_SHORT":          {"emoji": "🎯", "color": 15548997, "label": "RSI PROFILE — SHORT CONFIRMED"},
    "RSI_PROFILE_PENDING_LONG":   {"emoji": "🔵", "color": 3447003,  "label": "RSI PROFILE — LONG PENDING"},
    "RSI_PROFILE_PENDING_SHORT":  {"emoji": "🔵", "color": 15548997, "label": "RSI PROFILE — SHORT PENDING"},
    # SPY/VIX alerts
    "SPY_VIX_ENTRY":  {"emoji": "📊", "color": 3447003,  "label": "SPY/VIX SIGNAL"},
    "SPY_VIX_TP":     {"emoji": "🎯", "color": 5763719,  "label": "SPY/VIX TP HIT"},
    # News
    "NEWS_IMPACT":    {"emoji": "📰", "color": 16750848, "label": "NEWS ALERT"},
    # ADR target alerts
    "ADR_R75":        {"emoji": "📍", "color": 3447003,  "label": "ADR 75% TARGET"},
    "ADR_R100":       {"emoji": "🎯", "color": 16750848, "label": "ADR 100% — FULL RANGE"},
    "ADR_R125":       {"emoji": "🏆", "color": 15844367, "label": "ADR 125% — EXTENDED"},
    "ADR_S75":        {"emoji": "📍", "color": 3447003,  "label": "ADR 75% TARGET"},
    "ADR_S100":       {"emoji": "🎯", "color": 16750848, "label": "ADR 100% — FULL RANGE"},
    "ADR_S125":       {"emoji": "🏆", "color": 15844367, "label": "ADR 125% — EXTENDED"},
    # Fallback
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

    # Traffic light (hold/bank signal)
    traffic = alert_data.get("traffic", "")
    if traffic:
        lines.append(f"│  {traffic}")

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

        # CVD Flow line (candle color + momentum)
        candle_color = alert_data.get("candle_color", "")
        cvd_mom = alert_data.get("cvd_mom", 0)
        if candle_color or cvd_mom:
            color_icons = {"STRONG_BULL": "🟢", "WEAK_BULL": "🔵", "STRONG_BEAR": "🔴", "WEAK_BEAR": "🟠", "NEUTRAL": "⚪"}
            c_icon = color_icons.get(candle_color, "")
            c_short = candle_color.replace("_", " ").title() if candle_color else ""
            flow_str = f"Flow: {c_icon} {c_short}" if c_icon else ""
            if cvd_mom:
                flow_str += f" │ Mom: {cvd_mom:+.0f}" if flow_str else f"Mom: {cvd_mom:+.0f}"
            if flow_str:
                lines.append(flow_str)

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

    # Add chart link for quick access (only for actionable alerts)
    actionable = ["ENTRY", "RE_ENTRY", "REVERSAL", "MILESTONE_UP", "MILESTONE_DOWN", "BANK_EXIT",
                   "SPY_VIX_ENTRY", "RSI_PROFILE_LONG", "RSI_PROFILE_SHORT"]
    if CHART_URL and atype in actionable:
        description += f"\n\n📊 [Live Chart]({CHART_URL})"

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
    db_ok = False
    db_count = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM alerts")
        db_count = c.fetchone()[0]
        conn.close()
        db_ok = True
    except Exception:
        pass
    return jsonify({
        "status": "running", "service": "OneMP11 Alert Server",
        "version": "2.2 (V8.1b + SPY/VIX + RSI Profile)", "claude": ENABLE_CLAUDE,
        "discord": bool(DISCORD_WEBHOOK_URL), "db_ok": db_ok, "db_path": DB_PATH,
        "db_alerts": db_count, "stats_7d": get_stats(7)
    })


def process_alert_background(raw, source):
    """Background thread: Claude analysis + DB store + Discord forward"""
    try:
        if source == "SPY_VIX":
            alert_data = parse_spyvix_alert(raw)
        elif source == "RSI_PROFILE":
            alert_data = parse_rsi_profile_alert(raw)
        else:
            alert_data = parse_alert(raw)

        recent = get_recent_alerts(10)

        claude_analysis, claude_confidence = "", ""
        skip_types = ["TREND_OVER"]
        if alert_data["alert_type"] not in skip_types:
            claude_analysis, claude_confidence = analyze_with_claude(alert_data, recent)

        alert_data["claude_analysis"] = claude_analysis
        alert_data["claude_confidence"] = claude_confidence

        store_alert(alert_data)
        forward_to_discord(alert_data, claude_analysis, claude_confidence)
        print(f"BG: Processed {source}/{alert_data['alert_type']} → {claude_confidence or 'no analysis'}")
    except Exception as e:
        print(f"BG ERROR processing {source}: {e}")
        traceback.print_exc()
        # Still try to forward raw alert to Discord even if Claude fails
        try:
            if source == "SPY_VIX":
                alert_data = parse_spyvix_alert(raw)
            elif source == "RSI_PROFILE":
                alert_data = parse_rsi_profile_alert(raw)
            else:
                alert_data = parse_alert(raw)
            store_alert(alert_data)
            forward_to_discord(alert_data)
        except Exception:
            pass


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.get_json(force=True)
    except Exception:
        try:
            raw = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400

    # Detect source immediately (fast — no API calls)
    source = detect_source(raw)

    # Return 200 OK to TradingView IMMEDIATELY — process in background
    t = threading.Thread(target=process_alert_background, args=(raw, source), daemon=True)
    t.start()

    return jsonify({"status": "accepted", "source": source}), 200


@app.route("/test-webhook", methods=["POST"])
def test_webhook():
    try:
        raw = request.get_json(force=True)
    except Exception:
        try:
            raw = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400

    alert_data = parse_alert(raw) if detect_source(raw) == "V8_1B" else (parse_spyvix_alert(raw) if detect_source(raw) == "SPY_VIX" else parse_rsi_profile_alert(raw))
    recent = get_recent_alerts(10)

    claude_analysis, claude_confidence = "", ""
    skip_types = ["TREND_OVER"]
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


@app.route("/download-db", methods=["GET"])
def download_db():
    """Download the SQLite database file for offline analysis"""
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Add ?secret=your_webhook_secret to download"}), 403
    try:
        import shutil
        from flask import send_file
        # Copy to temp file (avoid locking issues)
        tmp_path = DB_PATH + ".download"
        shutil.copy2(DB_PATH, tmp_path)
        return send_file(tmp_path, as_attachment=True, download_name="onemp11_alerts.db",
                         mimetype="application/x-sqlite3")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db-stats", methods=["GET"])
def db_stats():
    """Quick database health check — row counts by source and type"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM alerts")
        total = c.fetchone()[0]
        c.execute("SELECT source, COUNT(*) FROM alerts GROUP BY source")
        by_source = {row[0] or "V8_1B": row[1] for row in c.fetchall()}
        c.execute("SELECT alert_type, COUNT(*) FROM alerts GROUP BY alert_type ORDER BY COUNT(*) DESC")
        by_type = {row[0]: row[1] for row in c.fetchall()}
        c.execute("SELECT MIN(timestamp), MAX(timestamp) FROM alerts")
        date_range = c.fetchone()
        conn.close()
        return jsonify({
            "total_alerts": total,
            "by_source": by_source,
            "by_type": by_type,
            "first_alert": date_range[0],
            "last_alert": date_range[1],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
                json={"model": "claude-sonnet-4-6", "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]},
                timeout=30
            )
            if response.status_code == 200:
                return jsonify({"summary": response.json()["content"][0]["text"], "stats": stats_data, "sessions": session_data})
        except Exception as e:
            print(f"Weekly summary error: {e}")

    return jsonify({"stats": stats_data, "sessions": session_data, "summary": "Claude unavailable"})


# News thread started via gunicorn.conf.py post_fork hook

if __name__ == "__main__":
    start_news_thread()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

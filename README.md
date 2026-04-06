# ⚡ OneMP11 Alert Server V2

Intercepts TradingView alerts, enriches them with **database-trained Claude AI analysis**, and forwards to Discord.

> **V2: No Yahoo Finance dependency.** Claude learns exclusively from your alert history database.

## What It Does

```
TradingView Alert → Your Server → Claude Analysis (DB-Trained) → Discord
                            ↓
                      SQLite Database
                    (stores everything)
                            ↓
                    Pattern Analysis
                  (Claude learns over time)
```

Every alert gets:
- 🤖 AI analysis trained on YOUR trade history (win rates, patterns, streaks)
- 🟢🟡🔴 Confidence rating (HIGH/MEDIUM/LOW) based on historical edge
- 📊 Session performance context (which hours you trade best)
- 💾 Stored for weekly performance review

## How Claude Learns

Claude receives the **full OneMP11 V8.1b knowledge base** with every analysis:
- Signal generation rules (CVD Flow, Kalman VWAP, ADX filtering)
- VIX regime tuning guide (High/Normal/Low settings)
- All 12 alert types and what they mean
- TL spread classification (TIGHT/RIDING/EXTENDED/STRETCHED)
- Session windows and their historical edge

Plus **your database history**:
- Win/loss rates by direction (LONG vs SHORT)
- Win rates by TL state (TIGHT entries vs EXTENDED)
- Win rates by ADX level (trending vs choppy)
- Claude's own confidence accuracy tracking
- Current streak detection

## Setup (10 minutes)

### 1. Deploy to Railway

1. Go to [railway.app](https://railway.app) and sign up (GitHub login)
2. Click **New Project** → **Deploy from GitHub repo**
3. Connect your GitHub and select this repo
4. Railway will auto-detect Python and deploy

### 2. Set Environment Variables

In Railway dashboard → your project → **Variables** tab:

| Variable | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL |
| `ANTHROPIC_API_KEY` | Your Anthropic API key (from console.anthropic.com) |
| `ENABLE_CLAUDE` | `true` |
| `WEBHOOK_SECRET` | `onemp11` (or any password) |

### 3. Get Your Server URL

Railway gives you a URL like: `https://onemp11-server-production.up.railway.app`

### 4. Update TradingView Alerts

In TradingView → Alerts → Edit each alert:
- **Webhook URL:** `https://YOUR-RAILWAY-URL.up.railway.app/webhook`
- Keep the alert message as-is (the Discord JSON format)

### 5. Test It

Visit `https://YOUR-RAILWAY-URL.up.railway.app/` — you should see:

```json
{
  "status": "running",
  "service": "OneMP11 Alert Server V2",
  "version": "2.0 (DB-Trained)",
  "claude_enabled": true,
  "discord_configured": true,
  "market_data": "removed (database-driven)"
}
```

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check + last 7 days stats |
| `/webhook` | POST | Receive TradingView alerts |
| `/test-webhook` | POST | Test alert (Discord only, no DB store) |
| `/alerts?n=20` | GET | View last N alerts |
| `/stats?days=7` | GET | Performance stats for N days |
| `/sessions` | GET | Performance by session window (8-10, 10-12, etc.) |
| `/knowledge` | GET | View system knowledge + pattern analysis |
| `/weekly-summary` | GET | Weekly AI-generated performance summary |

## What Changed in V2

| V1 | V2 |
|---|---|
| Yahoo Finance for market data | Removed (was unreliable on cloud) |
| Claude gets generic market snapshot | Claude gets YOUR trade history patterns |
| No session breakdown | Full session performance tracking |
| No pattern learning | Tracks win rates by direction, TL state, ADX |
| Claude confidence not tracked | Tracks Claude's own accuracy |

## Costs

| Service | Monthly |
|---|---|
| Railway | $5 (Hobby plan) |
| Claude API | ~$2-5 (Sonnet, ~500 calls) |
| **Total** | **~$7-10/mo** |

## File Structure

```
onemp11-server/
├── main.py              # Flask server (everything in one file)
├── requirements.txt     # flask, requests, gunicorn (no yfinance!)
├── Procfile             # Heroku/Railway process config
├── railway.json         # Railway deploy settings
├── .env.example         # Environment variable template
└── README.md            # This file
```

## ⚡ © 2026 OneMP11

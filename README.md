# ⚡ OneMP11 Alert Server

Intercepts TradingView alerts, enriches them with Claude AI analysis, and forwards to Discord.

## What It Does

```
TradingView Alert → Your Server → Claude Analysis → Discord
                         ↓
                   SQLite Database
                   (stores everything)
```

Every alert gets:
- 🤖 AI analysis of whether the signal supports the trend
- 🟢🟡🔴 Confidence rating (HIGH/MEDIUM/LOW)
- 📊 Historical context from recent alerts
- 💾 Stored for weekly performance review

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
- **Webhook URL**: `https://YOUR-RAILWAY-URL.up.railway.app/webhook`
- Keep the alert message as-is (the Discord JSON format)

### 5. Test It

Visit `https://YOUR-RAILWAY-URL.up.railway.app/` — you should see:
```json
{
  "status": "running",
  "service": "OneMP11 Alert Server",
  "claude_enabled": true,
  "discord_configured": true
}
```

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check + last 7 days stats |
| `/webhook` | POST | Receive TradingView alerts |
| `/alerts?n=20` | GET | View last N alerts |
| `/stats?days=7` | GET | Performance stats for N days |
| `/summary` | GET | Weekly AI-generated performance summary |

## Discord Alert Example

**Before (raw TradingView):**
```
🟢 GO LONG ES1! @ $6,600.75
🏄 TL: +8.5pts RIDING
RSI: 55 │ VIX: 38 │ CL: 60 │ ADX: 35
9:30 AM CST
```

**After (with Claude analysis):**
```
🟢 GO LONG ES1! @ $6,600.75
🏄 TL: +8.5pts RIDING
RSI: 55 │ VIX: 38 │ CL: 60 │ ADX: 35
9:30 AM CST
━━━━━━━━━━━━━━━━━━━
🤖 CLAUDE 🟢 HIGH
ES RSI rising with falling VIX RSI confirms risk-on.
CL aligned at 60 supports the move. TL RIDING with
room to run — conditions favor this long.
```

## Weekly Summary

Visit `/summary` or set up a cron to post to Discord every Sunday:

```
📊 WEEKLY SUMMARY
Trades: 12 | Wins: 7 | Losses: 5 | Win Rate: 58.3%
Total P&L: +185.5pts | Avg: +15.5pts/trade
Streak: 2W

Solid week with Open and O/N sessions carrying performance.
Two midday losses dragged avg down. Consider monitoring
VIX RSI divergence before midday entries — both losses
showed VIX RSI rising before entry.
```

## Costs

| Service | Monthly |
|---|---|
| Railway | $5 (Hobby plan) |
| Claude API | ~$2-5 (Sonnet, ~500 calls) |
| **Total** | **~$7-10/mo** |

## File Structure

```
onemp11-server/
├── main.py           # Flask server (everything in one file)
├── requirements.txt  # Python dependencies
├── Procfile          # Railway start command
├── railway.json      # Railway config
├── .env.example      # Environment variables template
└── README.md         # This file
```

## ⚡ © 2026 OneMP11

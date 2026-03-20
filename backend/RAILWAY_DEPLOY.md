# Railway Deployment Guide — Market Risk Engine Backend

## Overview
Runs your FastAPI backend 24/7 on Railway so the scheduler never misses
a job when your laptop is closed. ~$5-10/month.

---

## Step 1 — Prepare your repo (5 min)

Copy these 3 files into your backend/ directory:
  backend/Dockerfile
  backend/railway.toml
  backend/.dockerignore

Make sure requirements.txt is current:
  cd backend && .venv/bin/pip freeze > requirements.txt

Push to GitHub:
  git add backend/Dockerfile backend/railway.toml backend/.dockerignore backend/requirements.txt
  git commit -m "Add Railway deployment files"
  git push

---

## Step 2 — Create Railway project (3 min)

1. Go to https://railway.app → sign in with GitHub
2. New Project → Deploy from GitHub repo
3. Select your portfolio_dashboard repo
4. Set Root Directory to: backend
5. Railway detects the Dockerfile and starts building

---

## Step 3 — Add persistent volume (2 min)

In Railway dashboard → your service → Volumes tab:
  Click "Add Volume"
  Mount path: /app/data
  Size: 1 GB

This persists:
  /app/data/portfolio.db          ← hedge snapshots, account data
  /app/data/broker_submission_log.json  ← order lifecycle
  /app/data/config.json           ← Composer credentials (written on first boot)

---

## Step 4 — Set environment variables (10 min)

In Railway dashboard → Variables tab, add ALL of these:

  # Alpaca
  ALPACA_API_KEY        = your_key
  ALPACA_API_SECRET     = your_secret
  ALPACA_ENV            = paper
  ALPACA_BASE_URL       = https://paper-api.alpaca.markets/v2
  ALPACA_DATA_URL       = https://data.alpaca.markets

  # Finnhub
  FINNHUB_API_KEY       = your_key

  # Composer credentials — paste your entire config.json as a single line
  # Get this by running: cat backend/config.json | python3 -m json.tool -c
  # (the -c flag compacts it to one line)
  PD_CONFIG_JSON        = {"composer_accounts":[{"email":"you@example.com","password":"yourpass","local_auth_token":"yourtoken"}],"settings":{}}

  # Database — points to the persistent volume
  DATABASE_URL          = sqlite:////app/data/portfolio.db

  # Auth token — copy from your local config.json
  PD_LOCAL_AUTH_TOKEN   = your_local_auth_token

  # CORS — add your frontend URL (Vercel etc) + keep localhost for local dev
  PD_ALLOWED_ORIGINS    = https://your-frontend.vercel.app,http://localhost:3000

  # Security
  PD_SECRET_KEY         = generate_a_random_32_char_string

To get your PD_CONFIG_JSON value, run this locally:
  python3 -c "import json; d=json.load(open('backend/config.json')); print(json.dumps(d))"

---

## Step 5 — Get your Railway URL

After deploy succeeds, Railway shows your URL:
  https://portfolio-dashboard-backend-production.up.railway.app

Test it:
  curl https://your-railway-url.up.railway.app/api/health

---

## Step 6 — Update frontend (5 min)

In EodReviewDashboard.tsx and HedgePositionManager.tsx, change line 3:

  FIND:    const API = "http://localhost:8000/api";
  REPLACE: const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

Create frontend/.env.production:
  NEXT_PUBLIC_API_URL=https://your-railway-url.up.railway.app/api

api.ts already reads from NEXT_PUBLIC_API_URL — no changes needed there.

---

## Step 7 — Seed the database on Railway (one time)

In Railway dashboard → your service → Shell tab, run:

  python3 -c "
  from app.database import init_db
  init_db()
  print('DB initialized')
  "

To copy your existing local database up to Railway, use the Railway CLI:
  npm install -g @railway/cli
  railway login
  railway run --service your-service python3 << 'PYEOF'
  import shutil, os
  # This copies from /tmp which you'd upload via railway cp
  print("DB path:", os.environ.get("DATABASE_URL"))
  PYEOF

Or simpler: just re-seed with the seed script since the snapshots are generated data.

---

## Step 8 — Verify scheduler is running

In Railway dashboard → Logs tab, look for:
  INFO:     Scheduler started.
  INFO:     Added job '_run_monitor' ...
  INFO:     Added job '_run_eod_submission' ...

You should see these on startup. Then at 3:00/3:15/3:25/3:35/3:45/4:30 PM PST
you'll see the scheduler firing daily.

---

## Day-to-day

Auto-deploy: git push to main triggers Railway rebuild. Data survives.
Logs: Railway dashboard → Logs (real-time streaming).
Cost: ~$5-8/month for a small always-on instance.
Local dev: unchanged — still runs on localhost:8000.

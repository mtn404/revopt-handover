# Deploy guide — Vercel (frontend) + Render (backend)

This document walks you through the deployment from a freshly-initialised local repo to a live URL. Time required: **~20 minutes** end-to-end, all on free tiers.

The repo is already configured (`vercel.json`, `render.yaml`, `.gitignore`, GitHub Actions workflow). All that's left is the click-through in your accounts.

---

## Prerequisites checklist

- [ ] A **GitHub** account (free) — github.com
- [ ] A **Vercel** account (free Hobby plan) — vercel.com — sign in with GitHub
- [ ] A **Render** account (free) — render.com — sign in with GitHub

That's it. No credit card needed for any of the three.

---

## Step 1 — Push to GitHub (5 min)

The local repo is already initialised with the first commit. You just need to push it to a GitHub remote.

**1.1** Go to https://github.com/new and create a new repository named **`revopt-mvp`**:
- Visibility: **Private** is fine (and recommended for now)
- ⚠️ **Do not** initialise with a README, .gitignore, or licence — the repo already has those
- Click **Create repository**

**1.2** Back in your terminal, in the `revopt-mvp` folder:

```bash
git remote add origin https://github.com/<your-username>/revopt-mvp.git
git push -u origin main
```

You'll be prompted to authenticate. Use a Personal Access Token (Settings → Developer Settings → PAT, with `repo` scope).

**1.3** Refresh the GitHub page — you should see all the files. Done.

---

## Step 2 — Deploy backend to Render (8 min)

The backend is FastAPI serving `data/snapshot.json` over a REST API. Render reads `render.yaml` at the repo root and builds it automatically.

**2.1** Go to https://dashboard.render.com → **New +** → **Blueprint**.

**2.2** Connect your GitHub account if prompted, then select the **`revopt-mvp`** repo.

**2.3** Render will detect `render.yaml` and propose creating one service:
- Name: `revopt-api`
- Runtime: Python 3.11
- Region: Frankfurt
- Plan: Free
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Click **Apply** to confirm.

**2.4** Render will build (~5 min on first deploy). When done, you'll get a URL like `https://revopt-api.onrender.com`. **Copy this URL** — you'll need it for the Vercel step.

**2.5** Test it: paste `https://revopt-api.onrender.com/v1/health` into your browser. You should get a JSON response like `{"status":"ok"}`. If you get a 503 or timeout, the service may still be spinning up — wait 30 seconds and refresh.

> ⚠️ **Free-tier quirk:** Render spins down the service after 15 minutes of inactivity. The first request after spindown takes ~30 seconds to wake. Fine for demos; if you want always-on for the meeting, the Starter plan is $7/mo.

---

## Step 3 — Deploy frontend to Vercel (5 min)

**3.1** Go to https://vercel.com/new.

**3.2** Import the **`revopt-mvp`** repo from your GitHub account.

**3.3** On the configuration screen:
- **Framework preset:** Next.js (Vercel auto-detects this)
- **Root directory:** leave as `./` (do not set it to `app/` or similar)
- **Build command:** leave default (`npm run build`)
- **Output directory:** leave default

**3.4** Expand **Environment Variables** and add:

| Key | Value |
|-----|-------|
| `NEXT_PUBLIC_API_BASE` | `https://revopt-api.onrender.com` *(use your actual Render URL from step 2.4)* |

**3.5** Click **Deploy**.

**3.6** First deploy takes ~2 min. When done you'll get a URL like `https://revopt-mvp-<random>.vercel.app`. Click it — you should see the dashboard.

---

## Step 4 — Verify end-to-end (2 min)

Open the Vercel URL and check:

- [ ] Dashboard loads with the Utilidex blue header
- [ ] KPI tiles populate (revenue, % PF, etc.)
- [ ] Dispatch chart renders
- [ ] Sidebar nav works (Dashboard / Forecasts / Dispatch / Ancillary / Revenue / Settings)
- [ ] Asset Selector dropdown is interactive

If the dashboard loads but data tiles are empty, the frontend can't reach the backend. Check:
1. Your Render URL is correct in the Vercel env var (no trailing slash)
2. The Render service is awake (visit `/v1/health` directly — wakes it if asleep)
3. Browser console for CORS errors (should not occur if backend `main.py` has CORS middleware)

---

## Step 5 — Custom domain (optional, 10 min)

If you want `revopt.utilidex.com` or similar instead of the random Vercel URL:

1. In **Vercel → Project → Settings → Domains**, add your custom domain.
2. Vercel shows DNS records to add at your registrar.
3. Propagation takes 5–60 minutes.
4. SSL is automatic.

For the meeting, the default `*.vercel.app` URL is fine.

---

## Step 6 — Subsequent deploys (every push)

You're done. From now on, every `git push` to `main` triggers automatic redeploys on both Vercel and Render. No further clicks.

```bash
# make a change
git add .
git commit -m "..."
git push
# Vercel + Render rebuild automatically (~1-2 min each)
```

---

## What's pre-wired but not yet active

- **GitHub Actions daily cron** (`.github/workflows/daily_refresh.yml`) — currently a heartbeat workflow. Will become a real live refresh once tasks #13–#15 land (BMRS data fetcher → forecast inference → LP solve → commit new snapshot).
- **Live model retraining** — pre-built models are in the snapshot. Monthly retrain is task #12 on the live stream.

For the dissertation meeting, neither is needed — the current frozen-snapshot deployment is sufficient.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Vercel build fails on `next build` | Run `npm run build` locally to reproduce; usually a TypeScript error in a fresh file |
| Render build fails on `pip install` | Pin Python version in `render.yaml` envVars (already done: 3.11.7) |
| Frontend renders but charts empty | API URL env var wrong, or Render service asleep — check `/v1/health` |
| 404 on `/v1/snapshot` | Backend deployed but missing data — check `backend/data/snapshot.json` is committed |
| CORS error in browser console | Backend missing CORS middleware — check `backend/main.py` |
| Vercel says "missing build script" | Wrong root directory selected — must be `./`, not `app/` |

---

## Once the live URL is up

Update the dissertation slides and your meeting prep doc with the live URL. Then you have a clickable demo for Gresham/Utilidex.

For the meeting, open the URL in a tab beforehand to wake the Render backend (avoids the 30s cold-start wait when you actually click).

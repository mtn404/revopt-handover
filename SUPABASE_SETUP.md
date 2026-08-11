# Supabase Storage setup — one-time bootstrap

The live pipeline persists state (parquets, models, proprietary inputs) in a
**private** Supabase Storage bucket. This page is the one-time setup you do
on a laptop with the data already on disk.

## 1. Create a Supabase project

1. Go to <https://supabase.com>, sign in (free tier is fine — 1 GB storage)
2. **New project** → name `bess-mvp` or similar → pick a region close to
   GitHub Actions runners (`eu-west-1` is good if you're in the UK)
3. Wait ~2 min for provisioning

## 2. Grab the two keys you'll need

From your project dashboard:

| Where in the dashboard | What it is | Used by |
|---|---|---|
| Settings → API → **Project URL** | `https://xxxx.supabase.co` | Both bootstrap and GH Actions |
| Settings → API → **service_role** key (under "Project API keys", click the eye to reveal) | A long JWT starting with `eyJ...` | Both bootstrap and GH Actions |

**Don't use the `anon` key.** That's the public client-side key and won't
have permission to write to the bucket.

## 3. Bootstrap the bucket

From your laptop, inside the MVP repo:

```bash
# PowerShell
$env:SUPABASE_URL = "https://xxxx.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "eyJ..."
py -3.13 -m pip install -r pipeline/requirements.txt
py -3.13 pipeline/bootstrap_supabase.py
```

```bash
# bash / WSL
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="eyJ..."
python -m pip install -r pipeline/requirements.txt
python pipeline/bootstrap_supabase.py
```

This will:

- Create the `bess-mvp` bucket (private — only accessible with your service-role key)
- Upload every parquet from `../clean-pipeline/data/processed/`
- Upload Spectron NBP gas, UKA carbon, DA prices from `../clean-pipeline/data/proprietary/`
- Upload the current `data/snapshot.json` to `snapshots/latest.json` and
  `snapshots/snapshot_YYYY-MM-DD.json`
- Write `models_cache/last_retrained.txt` with today's date as a sentinel

Total upload size: ~30 MB. Takes ~1 min on a normal connection.

## 4. Add the secrets to GitHub

In the MVP repo on GitHub:

`Settings → Secrets and variables → Actions → New repository secret`

Add three (the third is optional):

| Name | Value |
|---|---|
| `SUPABASE_URL` | `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | the long `eyJ...` JWT |
| `SUPABASE_BUCKET` *(optional)* | `bess-mvp` (default if not set) |

## 5. Trigger the workflow to verify

You can wait for tomorrow 08:30 UTC, or kick it off now:

```bash
gh workflow run "Daily snapshot refresh"
gh run watch
```

The first run should:
1. Pull every artifact down from Supabase
2. Pull 14 days of public data
3. Stamp `snapshot.json` with `pipeline_mode = "backtest"` (because the
   model-cache files and live solver aren't wired yet)
4. Push the snapshot + any changes back to Supabase
5. Commit the updated `snapshot.json` to the repo
6. Vercel auto-deploys

The dashboard's Topbar pill should still show **BACKTEST** but with a
fresh `Refreshed …` timestamp.

## Is it private?

Yes. Concretely:

- The bucket is created with `public: false`. Files have no public URL.
- Reading or writing requires the service-role JWT, which is encrypted at
  rest in GitHub Actions secrets and masked (`***`) in workflow logs.
- The service-role key bypasses Row Level Security, so don't ever embed
  it in client-side code. It only lives in:
  - GitHub Actions secrets (server-side, encrypted)
  - Your laptop's environment when bootstrapping (delete the env var when done)

Anyone with **push access to the GitHub repo** could write a workflow that
uses the secret to read the bucket. For a personal dissertation repo with
just you as the owner, that's not a real attack surface. For a shared repo
or a client deployment, generate a more scoped key (Supabase doesn't have
native scoped keys yet, so you'd put the secret behind a tighter access
control — environment-protected secrets, or a separate Supabase project).

## Operational tips

- **Refresh proprietary data**: drop new Spectron / UKA files into a folder
  on your laptop and re-run `bootstrap_supabase.py`. The upload is upsert,
  so it'll just replace the previous version.
- **Verify what's in the bucket**: Supabase dashboard → Storage → `bess-mvp`.
  You'll see `parquets/`, `models_cache/`, `proprietary/`, `snapshots/`.
- **Download a snapshot for inspection**: dashboard → Storage → click any
  object → "Download" button.
- **Rotate the key**: dashboard → Settings → API → "Generate new key" → update
  the GitHub secret. Old key becomes invalid immediately.

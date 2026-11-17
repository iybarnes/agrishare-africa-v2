---
title: AgriShare Africa
emoji: 🌱
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
---

# AgriShare Africa — MVP

A prototype agricultural platform for farm discovery, geospatial visualisation, preliminary viability
screening, farmer updates, non-binding investment-interest capture and agricultural marketplace listings.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q                          # optional: run the test suite
ADMIN_PASSWORD=choose-a-long-password streamlit run app.py
```

## Configuration

All settings are environment variables (on Hugging Face: **Settings → Variables and secrets**; on Streamlit
Cloud: **Settings → Secrets**, using `ADMIN_PASSWORD = "..."`).

| Variable | Required | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | **Yes, to use the admin area** | Without it the admin page stays disabled. Use a long random value; store it as a *secret*, never in code. |
| `CONTACT_EMAIL` | Before going live | Shown on the About & privacy page as the data-request contact. |
| `SEED_DEMO_DATA` | No (default `1`) | Set to `0` for a real launch so the three illustrative sample farms are not created. |
| `AGRISHARE_DB` | No | Database file path. Default: `/data/agrishare.db` if writable, else next to `app.py`, else the temp dir. |
| `PORT` | No (default `7860`) | Port to listen on. Render/Railway/Fly set this automatically. |

## Deploy

**Hugging Face Spaces (Docker)** — create a Space with the *Docker* SDK and upload the repository contents
(`app.py`, `Dockerfile`, … at the top level). Add `ADMIN_PASSWORD` as a **Secret**. Optionally enable
*persistent storage*: the app detects `/data` automatically.

**Streamlit Community Cloud** — push to GitHub, then *New app* → pick the repo, branch `main`, file `app.py`.
Add `ADMIN_PASSWORD` under *Secrets*. (The Dockerfile is not used there.)

**Any Docker host** (Render, Railway, Fly.io, a VPS):

```bash
docker build -t agrishare .
docker run -p 7860:7860 -e ADMIN_PASSWORD=... -e SEED_DEMO_DATA=0 -v agrishare-data:/data agrishare
```

## Before going live — checklist

- [ ] `ADMIN_PASSWORD` set as a secret; sign in once to confirm the admin area works.
- [ ] `SEED_DEMO_DATA=0` and `CONTACT_EMAIL` set.
- [ ] **Persistence decided.** SQLite on a free/ephemeral container is wiped on restart. Enable a persistent
      volume at `/data`, or migrate to managed PostgreSQL (e.g. Supabase). Download a backup from the admin
      area regularly.
- [ ] Put the app behind Cloudflare (or similar) for rate-limiting and DDoS protection. The built-in
      per-session submit cooldowns stop accidental duplicates, not a determined attacker.
- [ ] Have the About & privacy text reviewed by a lawyer for each country you serve (data protection,
      crowdfunding/securities, land-tenure and consumer-protection rules).
- [ ] Moderate: review new farms and listings in the admin area; set a farm's status to *Verified* only after
      real due diligence, or *Rejected* to hide it from the public.
- [ ] Check map-tile terms for your expected traffic: the public OpenStreetMap tile servers are for light use,
      and Esri imagery has its own terms. At scale use a commercial tile provider.

## How it behaves

- **Farm update codes.** Registering a farm shows a one-time secret code (only a hash is stored). Posting a
  field update requires that code (administrators are exempt), so strangers cannot add "evidence" to a farm.
- **Data visibility.** Farms, updates and marketplace listings are public. Investor interest (name, email,
  amount) is visible only in the admin area.
- **Currencies.** Each farm has its own currency; totals are shown per currency and never summed across them.
- **Safety.** User text is escaped everywhere it is rendered (Markdown, map popups); photo links must be
  `http(s)`; CSV exports neutralise spreadsheet formulas.

## Prototype limitations

- Sample farms are illustrative, not verified. Map pins are single GPS points, not surveyed boundaries.
- FarmScore is a transparent equal-weight calculation of self-reported inputs, not a validated agronomic or
  financial model. Areas are in acres.
- Investor submissions are non-binding expressions of interest. No shares are issued, no money is collected
  and no returns are promised.
- There are no user accounts: the admin password and per-farm update codes are the only access controls.

## Roadmap

1. PostgreSQL/PostGIS persistence and real user accounts with role-based access.
2. Boundary upload (GeoJSON/KML), documents and an evidence-backed verification workflow with audit trail.
3. Earth Engine / Copernicus vegetation and land monitoring; weather, soil and water integrations.
4. Hectare/acre toggle, translations (French, Portuguese, Swahili, Hausa) and mobile/offline field reporting.
5. Regulated investment-partner integration after legal structuring.

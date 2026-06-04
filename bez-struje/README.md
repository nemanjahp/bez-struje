# ⚡ Bez Struje Obaveštenja

Power outage notification service for Serbia. Scrapes [elektrodistribucija.rs](https://elektrodistribucija.rs) for planned outages, matches them against user-registered addresses, and sends alerts.

## Quick Start (Local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the server
uvicorn main:app --reload

# 3. Open the API docs
open http://localhost:8000/docs
```

The server will immediately scrape all regions on startup and repeat every 60 minutes.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info |
| GET | `/docs` | Interactive Swagger UI |
| GET | `/health` | DB stats |
| GET | `/regions` | List available regions |
| POST | `/subscribe` | Register email + address |
| GET | `/subscriptions?email=...` | List your subscriptions |
| DELETE | `/subscriptions/{id}?email=...` | Remove a subscription |
| GET | `/outages` | Browse all scraped outages |
| GET | `/outages/check?address=...&region=...` | Check an address instantly |
| POST | `/admin/scrape` | Manually trigger scrape |
| POST | `/admin/notify` | Manually trigger matching |

## Example Usage

### Subscribe to outage alerts
```bash
curl -X POST http://localhost:8000/subscribe \
  -H "Content-Type: application/json" \
  -d '{
    "email": "petar@example.com",
    "address_text": "Omoljica Milosa Crnjanskog",
    "region": "novi_sad"
  }'
```

### Quick-check an address
```bash
curl "http://localhost:8000/outages/check?address=Kozaracka%20Oplena&region=novi_sad"
```

## Available Regions

- `beograd` — Belgrade
- `novi_sad` — Vojvodina (Novi Sad, Pančevo, Subotica, Sombor...)
- `nis` — Niš area
- `kragujevac` — Kragujevac area
- `kraljevo` — Kraljevo area

## Deploy to Railway

1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Railway auto-detects Python and uses the Procfile
4. Set environment variable: `CHECK_INTERVAL=60`
5. Done — the app runs on Railway's provided URL

## Architecture (MVP)

```
main.py          ← Everything: API, scraper, matcher, scheduler
bez_struje.db    ← SQLite database (auto-created)
requirements.txt ← Python deps
Procfile         ← Railway entry point
```

## Roadmap

- [ ] Email notifications (SMTP / Resend)
- [ ] Push notifications (Firebase Cloud Messaging)
- [ ] React Native mobile app
- [ ] Better address matching (fuzzy / NLP)
- [ ] PostgreSQL on Railway (for production)
- [ ] User dashboard (web)

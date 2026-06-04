"""
Bez Struje Obaveštenja - MVP
Power outage notification service for Serbia.
Scrapes elektrodistribucija.rs for planned outages, matches user addresses, sends alerts.
"""

import os
import re
import sqlite3
import hashlib
import logging
from datetime import datetime, date
from contextlib import contextmanager
from typing import Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE = os.getenv("DATABASE_PATH", "bez_struje.db")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bez-struje")

# ---------------------------------------------------------------------------
# Data source URLs
# elektrodistribucija.rs publishes planned outages as static HTML tables.
# Belgrade has its own path; all other regions share a different path prefix.
# Dan_0 = today/current, Dan_1 = tomorrow/next day.
# ---------------------------------------------------------------------------

REGIONS = {
    "beograd": {
        "label": "Beograd",
        "urls": [
            "https://elektrodistribucija.rs/planirana-iskljucenja-beograd/Dan_0_Iskljucenja.htm",
            "https://elektrodistribucija.rs/planirana-iskljucenja-beograd/Dan_1_Iskljucenja.htm",
        ],
        "has_branch_col": False,
    },
    "novi_sad": {
        "label": "Novi Sad / Vojvodina",
        "urls": [
            "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/NoviSad_Dan_0_Iskljucenja.htm",
            "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/NoviSad_Dan_1_Iskljucenja.htm",
        ],
        "has_branch_col": True,
    },
    "nis": {
        "label": "Niš",
        "urls": [
            "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Nis_Dan_0_Iskljucenja.htm",
            "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Nis_Dan_1_Iskljucenja.htm",
        ],
        "has_branch_col": True,
    },
    "kragujevac": {
        "label": "Kragujevac",
        "urls": [
            "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kragujevac_Dan_0_Iskljucenja.htm",
            "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kragujevac_Dan_1_Iskljucenja.htm",
        ],
        "has_branch_col": True,
    },
    "kraljevo": {
        "label": "Kraljevo",
        "urls": [
            "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kraljevo_Dan_0_Iskljucenja.htm",
            "https://elektrodistribucija.rs/planirana-iskljucenja-srbija/Kraljevo_Dan_1_Iskljucenja.htm",
        ],
        "has_branch_col": True,
    },
}

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                address_text TEXT NOT NULL,
                municipality TEXT,
                region TEXT DEFAULT 'beograd',
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS outages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hash TEXT UNIQUE NOT NULL,
                region TEXT NOT NULL,
                municipality TEXT NOT NULL,
                time_range TEXT NOT NULL,
                streets_raw TEXT NOT NULL,
                outage_date TEXT NOT NULL,
                scraped_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
                outage_id INTEGER NOT NULL REFERENCES outages(id),
                sent_at TEXT DEFAULT (datetime('now')),
                channel TEXT DEFAULT 'log',
                UNIQUE(subscription_id, outage_id)
            );

            CREATE INDEX IF NOT EXISTS idx_outages_date ON outages(outage_date);
            CREATE INDEX IF NOT EXISTS idx_outages_hash ON outages(hash);
            CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id);
        """)
    log.info("Database initialized.")


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> Optional[str]:
    """Fetch a single outage page. Returns HTML or None on failure."""
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.5",
        })
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None


def parse_outage_page(html: str, region: str, has_branch: bool) -> list[dict]:
    """
    Parse an EDS outage HTML table into structured records.
    Belgrade tables have 3 columns: Општина | Време | Улице
    Regional tables have 4 columns: Огранак | Општина | Време | Улице
    """
    soup = BeautifulSoup(html, "html.parser")
    records = []

    # Extract date from the header row / first cell
    outage_date = str(date.today())
    header_cells = soup.find_all(["th", "td"], string=re.compile(r"\d{4}[-.]"))
    for cell in header_cells:
        text = cell.get_text(strip=True)
        # Try ISO format: 2026-06-03
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            outage_date = m.group(1)
            break
        # Try Serbian format: 04.06.2026
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if m:
            outage_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            break

    # Find the data table (skip the header/title table)
    tables = soup.find_all("table")
    data_table = None
    for t in tables:
        rows = t.find_all("tr")
        # The data table has rows with 3+ <td> elements
        for row in rows:
            tds = row.find_all("td")
            if len(tds) >= 3:
                data_table = t
                break
        if data_table:
            break

    if not data_table:
        return records

    for row in data_table.find_all("tr"):
        cells = row.find_all("td")

        # Skip header rows (contain <b> tags or known header text)
        if any(cell.find("b") for cell in cells):
            continue

        if has_branch and len(cells) >= 4:
            municipality = cells[1].get_text(strip=True)
            time_range = cells[2].get_text(strip=True)
            streets_raw = cells[3].get_text(strip=True)
        elif not has_branch and len(cells) >= 3:
            municipality = cells[0].get_text(strip=True)
            time_range = cells[1].get_text(strip=True)
            streets_raw = cells[2].get_text(strip=True)
        else:
            continue

        if not municipality or not streets_raw:
            continue

        # Create a unique hash so we don't store duplicates
        raw_str = f"{region}|{outage_date}|{municipality}|{time_range}|{streets_raw}"
        h = hashlib.sha256(raw_str.encode()).hexdigest()[:16]

        records.append({
            "hash": h,
            "region": region,
            "municipality": municipality,
            "time_range": time_range,
            "streets_raw": streets_raw,
            "outage_date": outage_date,
        })

    return records


def scrape_all_regions() -> int:
    """Scrape all regions and store new outages. Returns count of new records."""
    new_count = 0
    for region_key, cfg in REGIONS.items():
        for url in cfg["urls"]:
            html = fetch_page(url)
            if not html:
                continue
            records = parse_outage_page(html, region_key, cfg["has_branch_col"])
            with get_db() as db:
                for rec in records:
                    try:
                        db.execute(
                            """INSERT OR IGNORE INTO outages
                               (hash, region, municipality, time_range, streets_raw, outage_date)
                               VALUES (:hash, :region, :municipality, :time_range, :streets_raw, :outage_date)""",
                            rec,
                        )
                        if db.total_changes:
                            new_count += 1
                    except sqlite3.IntegrityError:
                        pass
            log.info(f"Scraped {url}: {len(records)} records parsed")
    log.info(f"Scrape complete. {new_count} new outage records stored.")
    return new_count


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lowercase, transliterate Cyrillic+Latin diacritics, strip punctuation."""
    text = text.lower().strip()
    # Step 1: Cyrillic → Latin (with diacritics)
    cyr_lat = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "dj",
        "е": "e", "ж": "z", "з": "z", "и": "i", "ј": "j", "к": "k",
        "л": "l", "љ": "lj", "м": "m", "н": "n", "њ": "nj", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "ћ": "c", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "c", "џ": "dz", "ш": "s",
    }
    result = []
    for ch in text:
        result.append(cyr_lat.get(ch, ch))
    text = "".join(result)
    # Step 2: Strip Latin diacritics (č→c, ć→c, š→s, ž→z, đ→dj)
    lat_strip = {"č": "c", "ć": "c", "š": "s", "ž": "z", "đ": "dj"}
    text = "".join(lat_strip.get(ch, ch) for ch in text)
    # Step 3: Remove punctuation except spaces, collapse whitespace
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def address_matches_outage(address: str, streets_raw: str, municipality: str) -> bool:
    """
    MVP matching: check if any significant word from the user's address
    appears in the outage streets text or municipality.
    We split the user address into tokens and check if at least 2 tokens
    (or 1 if the address is short) appear in the outage text.
    """
    addr_norm = normalize(address)
    outage_norm = normalize(streets_raw + " " + municipality)

    tokens = addr_norm.split()
    # Filter out very short / common words
    stop_words = {"ul", "ulica", "br", "broj", "bb", "i", "u", "na", "od", "do", "sa"}
    tokens = [t for t in tokens if len(t) > 1 and t not in stop_words]

    if not tokens:
        return False

    matches = sum(1 for t in tokens if t in outage_norm)
    threshold = min(2, len(tokens))  # need at least 2 tokens to match, or all if fewer
    return matches >= threshold


def check_and_notify():
    """Main job: find matching outages for all subscriptions and log notifications."""
    log.info("Running match & notify cycle...")
    today = date.today().isoformat()

    with get_db() as db:
        subs = db.execute(
            "SELECT * FROM subscriptions WHERE active = 1"
        ).fetchall()
        outages = db.execute(
            "SELECT * FROM outages WHERE outage_date >= ?", (today,)
        ).fetchall()

        notify_count = 0
        for sub in subs:
            for outage in outages:
                # Optionally filter by region
                if sub["region"] and sub["region"] != outage["region"]:
                    continue

                if address_matches_outage(
                    sub["address_text"], outage["streets_raw"], outage["municipality"]
                ):
                    # Check if already notified
                    existing = db.execute(
                        "SELECT 1 FROM notifications WHERE subscription_id=? AND outage_id=?",
                        (sub["id"], outage["id"]),
                    ).fetchone()
                    if existing:
                        continue

                    # MVP: log the notification (email/push comes later)
                    user = db.execute(
                        "SELECT email FROM users WHERE id=?", (sub["user_id"],)
                    ).fetchone()

                    log.warning(
                        f"🔴 OUTAGE ALERT for {user['email']}:\n"
                        f"   Address: {sub['address_text']}\n"
                        f"   Date: {outage['outage_date']}\n"
                        f"   Time: {outage['time_range']}\n"
                        f"   Municipality: {outage['municipality']}\n"
                        f"   Streets: {outage['streets_raw'][:120]}..."
                    )

                    db.execute(
                        "INSERT OR IGNORE INTO notifications (subscription_id, outage_id, channel) VALUES (?, ?, 'log')",
                        (sub["id"], outage["id"]),
                    )
                    notify_count += 1

    log.info(f"Notify cycle done. {notify_count} new alerts.")


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler()


def scheduled_scrape_and_notify():
    """Combined job: scrape then match."""
    try:
        scrape_all_regions()
        check_and_notify()
    except Exception as e:
        log.error(f"Scheduled job failed: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Bez Struje Obaveštenja",
    description="Power outage notifications for Serbia",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Pydantic models ---

class UserRegister(BaseModel):
    email: EmailStr

class SubscriptionCreate(BaseModel):
    email: EmailStr
    address_text: str
    municipality: Optional[str] = None
    region: str = "beograd"

class SubscriptionOut(BaseModel):
    id: int
    address_text: str
    municipality: Optional[str]
    region: str
    active: bool

class OutageOut(BaseModel):
    id: int
    region: str
    municipality: str
    time_range: str
    streets_raw: str
    outage_date: str


# --- Endpoints ---

@app.on_event("startup")
def startup():
    init_db()
    scheduler.add_job(
        scheduled_scrape_and_notify,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="scrape_and_notify",
        next_run_time=datetime.now(),  # run immediately on startup
    )
    scheduler.start()
    log.info(f"Scheduler started. Checking every {CHECK_INTERVAL_MINUTES} min.")


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)


@app.get("/")
def root():
    return {
        "service": "Bez Struje Obaveštenja",
        "version": "0.1.0",
        "status": "running",
        "docs": "/docs",
    }


@app.post("/subscribe", response_model=SubscriptionOut)
def subscribe(data: SubscriptionCreate):
    """Register an email + address to receive outage notifications."""
    with get_db() as db:
        # Upsert user
        db.execute("INSERT OR IGNORE INTO users (email) VALUES (?)", (data.email,))
        user = db.execute("SELECT id FROM users WHERE email=?", (data.email,)).fetchone()

        # Check for duplicate subscription
        existing = db.execute(
            "SELECT id FROM subscriptions WHERE user_id=? AND address_text=?",
            (user["id"], data.address_text),
        ).fetchone()
        if existing:
            raise HTTPException(400, "You already subscribed this address.")

        # Validate region
        if data.region not in REGIONS:
            raise HTTPException(400, f"Unknown region. Valid: {list(REGIONS.keys())}")

        db.execute(
            """INSERT INTO subscriptions (user_id, address_text, municipality, region)
               VALUES (?, ?, ?, ?)""",
            (user["id"], data.address_text, data.municipality, data.region),
        )
        sub = db.execute(
            "SELECT * FROM subscriptions WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()

    return SubscriptionOut(
        id=sub["id"],
        address_text=sub["address_text"],
        municipality=sub["municipality"],
        region=sub["region"],
        active=bool(sub["active"]),
    )


@app.get("/subscriptions")
def list_subscriptions(email: str):
    """List all subscriptions for an email."""
    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            raise HTTPException(404, "Email not found.")
        subs = db.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND active=1", (user["id"],)
        ).fetchall()
    return [
        SubscriptionOut(
            id=s["id"],
            address_text=s["address_text"],
            municipality=s["municipality"],
            region=s["region"],
            active=bool(s["active"]),
        )
        for s in subs
    ]


@app.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, email: str):
    """Deactivate a subscription."""
    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            raise HTTPException(404, "Email not found.")
        result = db.execute(
            "UPDATE subscriptions SET active=0 WHERE id=? AND user_id=?",
            (sub_id, user["id"]),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Subscription not found.")
    return {"status": "deactivated", "id": sub_id}


@app.get("/outages", response_model=list[OutageOut])
def list_outages(
    region: Optional[str] = None,
    municipality: Optional[str] = None,
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
):
    """List scraped outages with optional filters."""
    query = "SELECT * FROM outages WHERE 1=1"
    params = []

    if region:
        query += " AND region = ?"
        params.append(region)
    if municipality:
        query += " AND municipality LIKE ?"
        params.append(f"%{municipality}%")
    if date_from:
        query += " AND outage_date >= ?"
        params.append(date_from)
    else:
        query += " AND outage_date >= ?"
        params.append(date.today().isoformat())

    query += " ORDER BY outage_date, time_range LIMIT 200"

    with get_db() as db:
        rows = db.execute(query, params).fetchall()

    return [
        OutageOut(
            id=r["id"],
            region=r["region"],
            municipality=r["municipality"],
            time_range=r["time_range"],
            streets_raw=r["streets_raw"],
            outage_date=r["outage_date"],
        )
        for r in rows
    ]


@app.get("/outages/check")
def check_address(
    address: str,
    region: str = "beograd",
):
    """Check if a given address has any upcoming outages (no registration needed)."""
    today = date.today().isoformat()
    with get_db() as db:
        outages = db.execute(
            "SELECT * FROM outages WHERE region=? AND outage_date >= ?",
            (region, today),
        ).fetchall()

    matches = []
    for o in outages:
        if address_matches_outage(address, o["streets_raw"], o["municipality"]):
            matches.append(
                OutageOut(
                    id=o["id"],
                    region=o["region"],
                    municipality=o["municipality"],
                    time_range=o["time_range"],
                    streets_raw=o["streets_raw"],
                    outage_date=o["outage_date"],
                )
            )
    return {"address": address, "region": region, "outages_found": len(matches), "outages": matches}


@app.post("/admin/scrape")
def trigger_scrape():
    """Manually trigger a scrape (for testing / admin)."""
    new_count = scrape_all_regions()
    return {"status": "done", "new_records": new_count}


@app.post("/admin/notify")
def trigger_notify():
    """Manually trigger matching & notification (for testing / admin)."""
    check_and_notify()
    return {"status": "done"}


@app.get("/regions")
def list_regions():
    """List available regions for subscription."""
    return {k: v["label"] for k, v in REGIONS.items()}


@app.get("/health")
def health():
    with get_db() as db:
        outage_count = db.execute("SELECT COUNT(*) as c FROM outages").fetchone()["c"]
        user_count = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        sub_count = db.execute("SELECT COUNT(*) as c FROM subscriptions WHERE active=1").fetchone()["c"]
    return {
        "status": "ok",
        "outages_in_db": outage_count,
        "users": user_count,
        "active_subscriptions": sub_count,
    }

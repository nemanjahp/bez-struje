"""
Bez Struje Obaveštenja — v2
Power outage notification service for Belgrade, Serbia.
Scrapes elektrodistribucija.rs, matches user addresses, sends email alerts
2 days and 1 day before a planned outage.
"""

import os
import re
import sqlite3
import hashlib
import logging
from datetime import datetime, date, timedelta
from contextlib import contextmanager
from typing import Optional
from pathlib import Path

import requests
import resend
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE = os.getenv("DATABASE_PATH", "bez_struje.db")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Bez Struje <obavestenja@bezstruje.rs>")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bez-struje")

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# ---------------------------------------------------------------------------
# Data source URLs — Belgrade only for MVP, more regions later
# ---------------------------------------------------------------------------

MAX_DAYS_AHEAD = int(os.getenv("MAX_DAYS_AHEAD", "7"))

def _build_urls(prefix: str, slug: str) -> list[str]:
    return [
        f"https://elektrodistribucija.rs/{prefix}/{slug}Dan_{i}_Iskljucenja.htm"
        for i in range(MAX_DAYS_AHEAD)
    ]

REGIONS = {
    "beograd": {
        "label": "Beograd",
        "urls": _build_urls("planirana-iskljucenja-beograd", ""),
        "has_branch_col": False,
    },
}

# ---------------------------------------------------------------------------
# Database
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
                street TEXT NOT NULL,
                house_number TEXT,
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
                notification_type TEXT NOT NULL,
                sent_at TEXT DEFAULT (datetime('now')),
                channel TEXT DEFAULT 'log',
                UNIQUE(subscription_id, outage_id, notification_type)
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
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.5",
        })
        resp.raise_for_status()
        # EDS pages sometimes lack charset header — force proper detection
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except requests.RequestException as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None


def parse_outage_page(html: str, region: str, has_branch: bool) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []

    # Extract date from header
    outage_date = str(date.today())
    header_cells = soup.find_all(["th", "td"], string=re.compile(r"\d{4}[-.]"))
    for cell in header_cells:
        text = cell.get_text(strip=True)
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            outage_date = m.group(1)
            break
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if m:
            outage_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            break

    # Find data table
    tables = soup.find_all("table")
    data_table = None
    for t in tables:
        for row in t.find_all("tr"):
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
            log.info(f"Scraped {url}: {len(records)} records")
    log.info(f"Scrape complete. {new_count} new outage records.")
    return new_count


# ---------------------------------------------------------------------------
# Text normalization — Cyrillic/Latin agnostic
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    text = text.lower().strip()
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
    lat_strip = {"č": "c", "ć": "c", "š": "s", "ž": "z", "đ": "dj"}
    text = "".join(lat_strip.get(ch, ch) for ch in text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Matching engine — Belgrade format: "УЛИЦА: бројеви, УЛИЦА2: бројеви"
# ---------------------------------------------------------------------------

def parse_number(num_str: str) -> Optional[int]:
    """Extract the numeric part from a house number like '35Ф', '8', 'ББ'."""
    m = re.match(r"(\d+)", num_str.strip())
    return int(m.group(1)) if m else None


def number_in_range_str(house_num: str, range_str: str) -> bool:
    """
    Check if a house number matches a range string.
    Examples: '26-28' matches 26,27,28. '8' matches 8. 'ББ' matches anything.
    """
    range_str = range_str.strip()
    if not range_str:
        return False

    # "ББ" or "BB" (bez broja) — matches everything
    if normalize(range_str) in ("bb", ""):
        return True

    user_num = parse_number(house_num)
    if user_num is None:
        return False

    # Range like "26-28" or "29-35Ф"
    range_match = re.match(r"(\d+)\s*[-–]\s*(\d+)", range_str)
    if range_match:
        low = int(range_match.group(1))
        high = int(range_match.group(2))
        return low <= user_num <= high

    # Single number like "8" or "35Ф"
    single = parse_number(range_str)
    if single is not None:
        return user_num == single

    return False


def parse_streets_from_raw(streets_raw: str) -> dict[str, str]:
    """
    Parse Belgrade format: "УЛИЦА: 22,26-28, ДРУГА УЛИЦА: ББ,8,"
    Returns {normalized_street_name: "original numbers string"}
    """
    result = {}

    # Split on pattern: ", STREETNAME:" where STREETNAME is before a colon
    # Strategy: find all "NAME: numbers" chunks by splitting on colons
    # and working backwards from each colon to find the street name
    parts = re.split(r":\s*", streets_raw)

    for i in range(len(parts) - 1):
        # The street name is at the end of parts[i] (after the last comma)
        # The numbers are in parts[i+1] (up to the start of the next street name)
        chunk = parts[i]
        # Street name is the last comma-separated segment
        segments = chunk.rsplit(",", 1)
        street_name = segments[-1].strip() if segments else chunk.strip()

        # Numbers are everything in the next part (which will be trimmed by the next iteration)
        numbers = parts[i + 1].strip()

        if street_name:
            norm = normalize(street_name)
            result[norm] = numbers

    return result


def street_matches(user_street: str, streets_raw: str) -> bool:
    """Check if a user's street name appears in the outage streets_raw text."""
    norm_user = normalize(user_street)
    if not norm_user or len(norm_user) < 3:
        return False

    # Method 1: Parse structured "STREET: numbers" format
    parsed = parse_streets_from_raw(streets_raw)
    for norm_street in parsed:
        # Check if user's street is a substring of the parsed street or vice versa
        if norm_user in norm_street or norm_street in norm_user:
            return True

    # Method 2: Fallback — check if the normalized street appears anywhere in the text
    norm_raw = normalize(streets_raw)
    # Split user street into significant tokens
    tokens = [t for t in norm_user.split() if len(t) > 2]
    if len(tokens) >= 2:
        return all(t in norm_raw for t in tokens)
    elif len(tokens) == 1:
        return tokens[0] in norm_raw

    return False


def house_number_matches(user_house_num: str, user_street: str, streets_raw: str) -> bool:
    """
    Check if a specific house number is affected.
    If no house number provided, street match alone is enough.
    """
    if not user_house_num or not user_house_num.strip():
        return True  # No number specified = match on street alone

    norm_user_street = normalize(user_street)
    parsed = parse_streets_from_raw(streets_raw)

    for norm_street, numbers_str in parsed.items():
        if norm_user_street in norm_street or norm_street in norm_user_street:
            # Found matching street — check numbers
            number_parts = [p.strip() for p in numbers_str.split(",") if p.strip()]
            # Remove parts that look like the next street name (no digits)
            number_parts = [p for p in number_parts if re.search(r"\d|[Бб][Бб]|BB|bb", p)]

            if not number_parts:
                return True  # Street listed but no specific numbers = whole street affected

            for part in number_parts:
                # Handle compound like "29-35Ф/8" → split on "/"
                for sub in part.split("/"):
                    if number_in_range_str(user_house_num, sub):
                        return True

            return False  # Street matched but number not in listed ranges

    return True  # Fallback: couldn't parse structure, be safe and notify


def address_matches_outage(street: str, house_number: str, streets_raw: str, municipality: str) -> bool:
    """Full matching pipeline: street name → house number."""
    if not street_matches(street, streets_raw):
        return False
    return house_number_matches(house_number, street, streets_raw)


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send email via Resend. Returns True on success."""
    if not RESEND_API_KEY:
        log.warning(f"📧 EMAIL (no API key, logged only): To={to}, Subject={subject}")
        return False

    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": [to],
            "subject": subject,
            "html": html_body,
        })
        log.info(f"📧 Email sent to {to}: {subject}")
        return True
    except Exception as e:
        log.error(f"Failed to send email to {to}: {e}")
        return False


def build_outage_email(notification_type: str, outage: dict, sub: dict) -> tuple[str, str]:
    """Build email subject and HTML body for an outage notification."""
    days_label = "⚡ Sutra" if notification_type == "1_day" else "⚡ Za 2 dana"
    date_formatted = outage["outage_date"]

    # Parse date for nicer display
    try:
        dt = datetime.strptime(outage["outage_date"], "%Y-%m-%d")
        days_sr = ["ponedeljak", "utorak", "sreda", "četvrtak", "petak", "subota", "nedelja"]
        day_name = days_sr[dt.weekday()]
        date_formatted = f"{day_name}, {dt.strftime('%d.%m.%Y')}"
    except ValueError:
        pass

    addr_display = sub["street"]
    if sub.get("house_number"):
        addr_display += f" {sub['house_number']}"

    subject = f"{days_label} nestaje struja — {addr_display}"

    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 520px; margin: 0 auto; padding: 24px;">
        <div style="background: #1a1a2e; color: #fff; padding: 24px 28px; border-radius: 12px 12px 0 0;">
            <h1 style="margin: 0; font-size: 20px;">⚡ Bez Struje Obaveštenje</h1>
        </div>
        <div style="background: #fff; border: 1px solid #e5e7eb; border-top: none; padding: 28px; border-radius: 0 0 12px 12px;">
            <p style="font-size: 16px; color: #111; margin-top: 0;">
                Planiran je <strong>prekid struje</strong> na vašoj adresi:
            </p>
            <div style="background: #fef3c7; border-left: 4px solid #f59e0b; padding: 16px; border-radius: 6px; margin: 20px 0;">
                <p style="margin: 0 0 8px 0; font-size: 15px;"><strong>📍 Adresa:</strong> {addr_display}</p>
                <p style="margin: 0 0 8px 0; font-size: 15px;"><strong>📅 Datum:</strong> {date_formatted}</p>
                <p style="margin: 0 0 8px 0; font-size: 15px;"><strong>🕐 Vreme:</strong> {outage["time_range"]}</p>
                <p style="margin: 0; font-size: 15px;"><strong>🏘️ Opština:</strong> {outage["municipality"]}</p>
            </div>
            <p style="font-size: 13px; color: #6b7280; margin-bottom: 0;">
                Izvor: <a href="https://elektrodistribucija.rs" style="color: #2563eb;">elektrodistribucija.rs</a><br>
                <a href="{BASE_URL}/odjava?sub_id={sub['id']}&email={sub.get('email', '')}" style="color: #9ca3af;">Odjavi se</a>
            </p>
        </div>
    </div>
    """
    return subject, html


# ---------------------------------------------------------------------------
# Notification engine
# ---------------------------------------------------------------------------

def check_and_notify():
    """Match outages to subscriptions and send emails 2 days and 1 day before."""
    log.info("Running match & notify cycle...")
    today = date.today()

    with get_db() as db:
        subs = db.execute(
            "SELECT s.*, u.email FROM subscriptions s JOIN users u ON s.user_id = u.id WHERE s.active = 1"
        ).fetchall()

        # Get all future outages
        outages = db.execute(
            "SELECT * FROM outages WHERE outage_date >= ?", (today.isoformat(),)
        ).fetchall()

        notify_count = 0
        for sub in subs:
            for outage in outages:
                if sub["region"] and sub["region"] != outage["region"]:
                    continue

                if not address_matches_outage(
                    sub["street"],
                    sub["house_number"] or "",
                    outage["streets_raw"],
                    outage["municipality"],
                ):
                    continue

                # Determine which notifications to send
                try:
                    outage_dt = datetime.strptime(outage["outage_date"], "%Y-%m-%d").date()
                except ValueError:
                    continue

                days_until = (outage_dt - today).days

                notifications_to_send = []
                if days_until == 2:
                    notifications_to_send.append("2_days")
                elif days_until == 1:
                    notifications_to_send.append("1_day")
                elif days_until == 0:
                    notifications_to_send.append("today")

                for ntype in notifications_to_send:
                    # Check if already sent
                    existing = db.execute(
                        "SELECT 1 FROM notifications WHERE subscription_id=? AND outage_id=? AND notification_type=?",
                        (sub["id"], outage["id"], ntype),
                    ).fetchone()
                    if existing:
                        continue

                    # Build and send email
                    sub_dict = dict(sub)
                    outage_dict = dict(outage)
                    subject, html_body = build_outage_email(ntype, outage_dict, sub_dict)
                    channel = "email" if send_email(sub["email"], subject, html_body) else "log"

                    db.execute(
                        "INSERT OR IGNORE INTO notifications (subscription_id, outage_id, notification_type, channel) VALUES (?, ?, ?, ?)",
                        (sub["id"], outage["id"], ntype, channel),
                    )
                    notify_count += 1

    log.info(f"Notify cycle done. {notify_count} new alerts.")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler()

def scheduled_scrape_and_notify():
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
    description="Power outage notifications for Belgrade",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"


# --- Pydantic models ---

class SubscriptionCreate(BaseModel):
    email: EmailStr
    street: str
    house_number: Optional[str] = None
    municipality: Optional[str] = None

class SubscriptionOut(BaseModel):
    id: int
    street: str
    house_number: Optional[str]
    municipality: Optional[str]
    active: bool

class OutageOut(BaseModel):
    id: int
    region: str
    municipality: str
    time_range: str
    streets_raw: str
    outage_date: str


# --- Lifecycle ---

@app.on_event("startup")
def startup():
    init_db()
    scheduler.add_job(
        scheduled_scrape_and_notify, "interval",
        minutes=CHECK_INTERVAL_MINUTES, id="scrape_and_notify",
        next_run_time=datetime.now(),
    )
    scheduler.start()
    log.info(f"Scheduler started. Checking every {CHECK_INTERVAL_MINUTES} min.")

@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
def landing_page():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Bez Struje</h1><p>Static files not found.</p>")


# --- API ---

@app.post("/api/subscribe", response_model=SubscriptionOut)
def subscribe(data: SubscriptionCreate):
    """Register email + street + house number for outage alerts."""
    if not data.street or len(data.street.strip()) < 3:
        raise HTTPException(400, "Unesite ime ulice (minimum 3 karaktera).")

    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO users (email) VALUES (?)", (data.email,))
        user = db.execute("SELECT id FROM users WHERE email=?", (data.email,)).fetchone()

        # Check duplicate
        existing = db.execute(
            "SELECT id FROM subscriptions WHERE user_id=? AND street=? AND COALESCE(house_number,'')=?",
            (user["id"], data.street.strip(), (data.house_number or "").strip()),
        ).fetchone()
        if existing:
            raise HTTPException(400, "Već ste prijavljeni za ovu adresu.")

        db.execute(
            "INSERT INTO subscriptions (user_id, street, house_number, municipality, region) VALUES (?, ?, ?, ?, 'beograd')",
            (user["id"], data.street.strip(), (data.house_number or "").strip() or None, data.municipality),
        )
        sub = db.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()

    return SubscriptionOut(
        id=sub["id"], street=sub["street"], house_number=sub["house_number"],
        municipality=sub["municipality"], active=bool(sub["active"]),
    )


@app.get("/api/subscriptions")
def list_subscriptions(email: str):
    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            return []
        subs = db.execute("SELECT * FROM subscriptions WHERE user_id=? AND active=1", (user["id"],)).fetchall()
    return [
        SubscriptionOut(
            id=s["id"], street=s["street"], house_number=s["house_number"],
            municipality=s["municipality"], active=bool(s["active"]),
        ) for s in subs
    ]


@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, email: str):
    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            raise HTTPException(404, "Email nije pronađen.")
        result = db.execute("UPDATE subscriptions SET active=0 WHERE id=? AND user_id=?", (sub_id, user["id"]))
        if result.rowcount == 0:
            raise HTTPException(404, "Pretplata nije pronađena.")
    return {"status": "deactivated", "id": sub_id}


@app.get("/odjava")
def unsubscribe_page(sub_id: int, email: str):
    """Unsubscribe link from email."""
    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if user:
            db.execute("UPDATE subscriptions SET active=0 WHERE id=? AND user_id=?", (sub_id, user["id"]))
    return HTMLResponse("""
        <html><body style="font-family: sans-serif; text-align: center; padding: 60px;">
            <h2>✅ Uspešno ste se odjavili</h2>
            <p>Nećete više primati obaveštenja za ovu adresu.</p>
            <a href="/">← Nazad na početnu</a>
        </body></html>
    """)


@app.get("/api/outages", response_model=list[OutageOut])
def list_outages(
    municipality: Optional[str] = None,
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
):
    query = "SELECT * FROM outages WHERE region = 'beograd'"
    params = []
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

    return [OutageOut(id=r["id"], region=r["region"], municipality=r["municipality"],
                      time_range=r["time_range"], streets_raw=r["streets_raw"],
                      outage_date=r["outage_date"]) for r in rows]


@app.get("/api/check")
def check_address(street: str, house_number: Optional[str] = None):
    """Instant check: does this address have any upcoming outages?"""
    today = date.today().isoformat()
    with get_db() as db:
        outages = db.execute(
            "SELECT * FROM outages WHERE region='beograd' AND outage_date >= ?", (today,)
        ).fetchall()

    matches = []
    for o in outages:
        if address_matches_outage(street, house_number or "", o["streets_raw"], o["municipality"]):
            matches.append(OutageOut(
                id=o["id"], region=o["region"], municipality=o["municipality"],
                time_range=o["time_range"], streets_raw=o["streets_raw"],
                outage_date=o["outage_date"],
            ))
    return {"street": street, "house_number": house_number, "outages_found": len(matches), "outages": matches}


@app.post("/admin/scrape")
def trigger_scrape():
    new_count = scrape_all_regions()
    return {"status": "done", "new_records": new_count}

@app.post("/admin/rescrape")
def trigger_rescrape():
    """Delete all outages and scrape fresh (use after encoding fix)."""
    with get_db() as db:
        db.execute("DELETE FROM outages")
        db.execute("DELETE FROM notifications")
    new_count = scrape_all_regions()
    return {"status": "done", "new_records": new_count}

@app.post("/admin/notify")
def trigger_notify():
    check_and_notify()
    return {"status": "done"}

@app.get("/admin/debug")
def debug_data():
    """Show raw outage data and encoding info for debugging."""
    with get_db() as db:
        outages = db.execute("SELECT * FROM outages ORDER BY id LIMIT 20").fetchall()
    result = []
    for o in outages:
        raw = o["streets_raw"]
        norm = normalize(raw)
        parsed = parse_streets_from_raw(raw)
        result.append({
            "id": o["id"],
            "municipality_raw": o["municipality"],
            "municipality_normalized": normalize(o["municipality"]),
            "streets_raw": raw,
            "streets_normalized": norm,
            "parsed_streets": {k: v for k, v in parsed.items()},
            "outage_date": o["outage_date"],
            "test_match_gospodar_jovanova": street_matches("Gospodar Jovanova", raw),
            "test_match_visnjiceva": street_matches("Visnjiceva", raw),
        })
    return result

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

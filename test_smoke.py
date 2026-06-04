#!/usr/bin/env python3
"""
Quick smoke test — run with: python test_smoke.py
Starts the server is NOT required; this tests the core logic directly.
"""

import sys
import os

# Ensure we use a test database
os.environ["DATABASE_PATH"] = "test_bez_struje.db"

from main import (
    init_db, normalize, address_matches_outage,
    parse_outage_page, get_db, REGIONS
)

def test_normalize():
    assert normalize("БЕОГРАД") == "beograd"
    assert normalize("Козарачка") == "kozaracka"
    assert normalize("Миloša Црњанског") == "milosa crnjanskog"
    assert normalize("  ul.  Kneza Miloša  br. 5  ") == "ul kneza milosa br 5"
    print("✅ normalize() works")

def test_matching():
    streets = "Омољица: Милоша Црњанског од Балканске до М.Миливојевића"
    municipality = "Панчево"

    assert address_matches_outage("Omoljica Milosa Crnjanskog", streets, municipality)
    assert address_matches_outage("Панчево Омољица", streets, municipality)
    assert not address_matches_outage("Bulevar Kralja Aleksandra Beograd", streets, municipality)
    assert not address_matches_outage("Novi Sad centar", streets, municipality)
    print("✅ address_matches_outage() works")

def test_parse_belgrade():
    html = """
    <html><body>
    <table><tr><td colspan="3"><b>БЕОГРАД - Планирана искључења за датум: 2026-06-05</b></td></tr></table>
    <table>
      <tr><td><b>Општина</b></td><td><b>Време</b></td><td><b>Улице</b></td></tr>
      <tr><td>Земун</td><td>09:00 - 14:00</td><td>Насеље БАТАЈНИЦА: ЈОВАНА ДУЧИЋА: 1-15</td></tr>
      <tr><td>Вождовац</td><td>08:00 - 15:00</td><td>КНЕЗА МИЛОША: 10-30</td></tr>
    </table>
    </body></html>
    """
    records = parse_outage_page(html, "beograd", has_branch=False)
    assert len(records) == 2
    assert records[0]["municipality"] == "Земун"
    assert records[0]["outage_date"] == "2026-06-05"
    assert records[1]["municipality"] == "Вождовац"
    print(f"✅ parse_outage_page(belgrade) → {len(records)} records")

def test_parse_regional():
    html = """
    <html><body>
    <table><tr><td colspan="4"><b>НОВИ САД - Планирана искључења за датум: 04.06.2026.</b></td></tr></table>
    <table>
      <tr><td><b>Огранак</b></td><td><b>Општина</b></td><td><b>Време</b></td><td><b>Улице</b></td></tr>
      <tr><td>Панчево</td><td>Панчево</td><td>08:30 - 13:00</td><td>Омољица: Милоша Црњанског</td></tr>
    </table>
    </body></html>
    """
    records = parse_outage_page(html, "novi_sad", has_branch=True)
    assert len(records) == 1
    assert records[0]["municipality"] == "Панчево"
    assert records[0]["outage_date"] == "2026-06-04"
    print(f"✅ parse_outage_page(regional) → {len(records)} records")

def test_db_roundtrip():
    # Clean slate
    if os.path.exists("test_bez_struje.db"):
        os.remove("test_bez_struje.db")

    init_db()
    with get_db() as db:
        db.execute("INSERT INTO users (email) VALUES (?)", ("test@example.com",))
        db.execute(
            "INSERT INTO subscriptions (user_id, address_text, region) VALUES (1, 'Omoljica Milosa Crnjanskog', 'novi_sad')"
        )
        db.execute(
            """INSERT INTO outages (hash, region, municipality, time_range, streets_raw, outage_date)
               VALUES ('abc123', 'novi_sad', 'Панчево', '08:30-13:00', 'Омољица: Милоша Црњанског', '2026-06-05')"""
        )
        subs = db.execute("SELECT COUNT(*) as c FROM subscriptions").fetchone()
        outages = db.execute("SELECT COUNT(*) as c FROM outages").fetchone()
        assert subs["c"] == 1
        assert outages["c"] == 1

    os.remove("test_bez_struje.db")
    print("✅ DB roundtrip works")


if __name__ == "__main__":
    print("Running smoke tests...\n")
    test_normalize()
    test_matching()
    test_parse_belgrade()
    test_parse_regional()
    test_db_roundtrip()
    print("\n🎉 All tests passed!")

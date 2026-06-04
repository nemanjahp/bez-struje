#!/usr/bin/env python3
"""
Bez Struje — QA Agent
Automated end-to-end testing against a live or local instance.

Usage:
    python qa_agent.py                          # test localhost:8000
    python qa_agent.py https://bez-struje-production.up.railway.app   # test production
"""

import sys
import json
import time
import requests
from datetime import date, timedelta

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
TEST_EMAIL = f"qa-test-{int(time.time())}@bezstruje-test.rs"

passed = 0
failed = 0
warnings = 0


def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")
        if detail:
            print(f"     → {detail}")


def warn(name, detail=""):
    global warnings
    warnings += 1
    print(f"  ⚠️  {name}")
    if detail:
        print(f"     → {detail}")


def get(path, **kwargs):
    return requests.get(f"{BASE}{path}", timeout=10, **kwargs)


def post(path, **kwargs):
    return requests.post(f"{BASE}{path}", timeout=10, **kwargs)


def delete(path, **kwargs):
    return requests.delete(f"{BASE}{path}", timeout=10, **kwargs)


# =========================================================================
print(f"\n⚡ Bez Struje QA Agent")
print(f"   Target: {BASE}")
print(f"   Test email: {TEST_EMAIL}")
print(f"   Time: {date.today().isoformat()}\n")


# --- 1. Health & Basic Endpoints ---
print("─── 1. Health & Basic Endpoints ───")

r = get("/health")
test("GET /health returns 200", r.status_code == 200)
health = r.json()
test("/health has status=ok", health.get("status") == "ok")
test("/health has outages_in_db field", "outages_in_db" in health)
test("/health has users field", "users" in health)

r = get("/")
test("GET / returns landing page", r.status_code == 200 and "Bez" in r.text and "<html" in r.text)

r = get("/api/regions")
test("GET /api/regions returns 200", r.status_code == 200)
regions = r.json()
test("/api/regions has beograd", "beograd" in regions)
test("/api/regions has novi_sad", "novi_sad" in regions)
test("/api/regions has 5 regions", len(regions) == 5, f"Got {len(regions)}")


# --- 2. Outages Data ---
print("\n─── 2. Outages Data ───")

r = get("/api/outages")
test("GET /api/outages returns 200", r.status_code == 200)
outages = r.json()
outage_count = len(outages)
test(f"/api/outages has data ({outage_count} records)", outage_count > 0,
     "No outages in DB — run /admin/rescrape first" if outage_count == 0 else "")

if outage_count > 0:
    o = outages[0]
    test("Outage has required fields", all(k in o for k in ["id", "region", "municipality", "time_range", "streets_raw", "outage_date"]))
    test("Outage date is valid format", len(o["outage_date"]) == 10 and "-" in o["outage_date"])
    test("Municipality is not garbled", not o["municipality"].startswith("Ð"),
         f"Encoding broken: {o['municipality'][:30]}")

    # Check per-region
    regions_with_data = set(o["region"] for o in outages)
    test(f"Outages cover regions: {regions_with_data}", len(regions_with_data) >= 1)

r = get("/api/outages?region=beograd")
test("Filter by region=beograd works", r.status_code == 200)

r = get("/api/outages?region=nonexistent")
test("Filter by invalid region returns empty", r.status_code == 200 and r.json() == [])


# --- 3. Address Matching ---
print("\n─── 3. Address Matching ───")

# Find a real street from the outages to test matching
test_street = None
test_region = None
if outage_count > 0:
    for o in outages:
        raw = o["streets_raw"]
        # Try to extract a street name (text before a colon)
        if ":" in raw:
            street_part = raw.split(":")[0].strip()
            if len(street_part) > 3 and len(street_part) < 50:
                test_street = street_part
                test_region = o["region"]
                break

if test_street:
    print(f"  ℹ️  Testing with real street: '{test_street}' in {test_region}")

    r = get(f"/api/check?street={requests.utils.quote(test_street)}&region={test_region}")
    test("GET /api/check returns 200", r.status_code == 200)
    check = r.json()
    test(f"Real street matched ({check.get('outages_found', 0)} outages)", check.get("outages_found", 0) > 0,
         f"Street '{test_street}' should match but got 0")
else:
    warn("Skipping real street matching — no parseable streets in outages")

# Negative test — fake street
r = get("/api/check?street=Nepostojeća%20Ulica%20Xyz&region=beograd")
test("Fake street returns 0 matches", r.status_code == 200 and r.json().get("outages_found") == 0)

# Edge cases
r = get("/api/check?street=ab&region=beograd")
test("Very short street still returns 200", r.status_code == 200)

r = get("/api/check?street=&region=beograd")
test("Empty street handled gracefully", r.status_code in [200, 422])


# --- 4. Subscribe Flow ---
print("\n─── 4. Subscribe Flow ───")

# Subscribe
r = post("/api/subscribe", json={
    "email": TEST_EMAIL,
    "street": "QA Test Ulica",
    "house_number": "42",
    "region": "beograd",
})
test("POST /api/subscribe returns 200", r.status_code == 200)
sub = r.json()
test("Subscription has id", "id" in sub)
test("Subscription street correct", sub.get("street") == "QA Test Ulica")
test("Subscription house_number correct", sub.get("house_number") == "42")
test("Subscription region correct", sub.get("region") == "beograd")
test("Subscription is active", sub.get("active") is True)
sub_id = sub.get("id")

# Duplicate check
r = post("/api/subscribe", json={
    "email": TEST_EMAIL,
    "street": "QA Test Ulica",
    "house_number": "42",
    "region": "beograd",
})
test("Duplicate subscription returns 400", r.status_code == 400)

# Subscribe second address
r = post("/api/subscribe", json={
    "email": TEST_EMAIL,
    "street": "Druga Ulica",
    "region": "novi_sad",
})
test("Second subscription works", r.status_code == 200)
sub2_id = r.json().get("id")

# List subscriptions
r = get(f"/api/subscriptions?email={TEST_EMAIL}")
test("GET /api/subscriptions returns 200", r.status_code == 200)
subs = r.json()
test(f"User has 2 subscriptions", len(subs) == 2, f"Got {len(subs)}")

# Invalid email
r = get("/api/subscriptions?email=nonexistent@nowhere.com")
test("Unknown email returns empty list", r.status_code == 200 and r.json() == [])

# Validation
r = post("/api/subscribe", json={
    "email": TEST_EMAIL,
    "street": "ab",
    "region": "beograd",
})
test("Short street rejected", r.status_code == 400)

r = post("/api/subscribe", json={
    "email": TEST_EMAIL,
    "street": "Neka Ulica",
    "region": "invalid_region",
})
test("Invalid region rejected", r.status_code == 400)


# --- 5. Unsubscribe Flow ---
print("\n─── 5. Unsubscribe Flow ───")

r = delete(f"/api/subscriptions/{sub_id}?email={TEST_EMAIL}")
test("DELETE subscription returns 200", r.status_code == 200)

r = get(f"/api/subscriptions?email={TEST_EMAIL}")
test("After delete, 1 subscription left", len(r.json()) == 1)

# Delete wrong user
r = delete(f"/api/subscriptions/{sub2_id}?email=wrong@email.com")
test("Delete with wrong email returns 404", r.status_code == 404)

# Unsubscribe via web link
r = get(f"/odjava?sub_id={sub2_id}&email={TEST_EMAIL}")
test("GET /odjava returns HTML", r.status_code == 200 and "Uspešno" in r.text)

r = get(f"/api/subscriptions?email={TEST_EMAIL}")
test("After odjava, 0 subscriptions left", len(r.json()) == 0)


# --- 6. Admin Endpoints ---
print("\n─── 6. Admin Endpoints ───")

r = post("/admin/scrape")
test("POST /admin/scrape returns 200", r.status_code == 200)
test("/admin/scrape returns new_records field", "new_records" in r.json())

r = post("/admin/notify")
test("POST /admin/notify returns 200", r.status_code == 200)

r = get("/admin/debug")
test("GET /admin/debug returns 200", r.status_code == 200)
if r.status_code == 200 and len(r.json()) > 0:
    d = r.json()[0]
    test("Debug has streets_normalized", "streets_normalized" in d)
    test("Debug has parsed_streets", "parsed_streets" in d)


# --- 7. Encoding & Matching Quality ---
print("\n─── 7. Encoding & Matching Quality ───")

if outage_count > 0:
    # Check that Cyrillic is properly stored
    sample = outages[0]
    muni = sample["municipality"]
    is_cyrillic = any("\u0400" <= c <= "\u04FF" for c in muni)
    is_latin = any("a" <= c.lower() <= "z" for c in muni)
    test("Municipality is readable (Cyrillic or Latin)", is_cyrillic or is_latin,
         f"Got: {muni[:40]}")

    # Test Latin/Cyrillic normalization consistency
    from urllib.parse import quote
    if test_street:
        # Test with same street in different encodings
        r1 = get(f"/api/check?street={quote(test_street)}&region={test_region}")
        count1 = r1.json().get("outages_found", 0)
        test(f"Matching works with original text ({count1} matches)", count1 > 0)


# --- 8. Performance ---
print("\n─── 8. Performance ───")

start = time.time()
r = get("/health")
health_ms = (time.time() - start) * 1000
test(f"/health responds in <500ms ({health_ms:.0f}ms)", health_ms < 500)

start = time.time()
r = get("/api/outages")
outages_ms = (time.time() - start) * 1000
test(f"/api/outages responds in <2000ms ({outages_ms:.0f}ms)", outages_ms < 2000)

start = time.time()
r = get("/api/check?street=Test&region=beograd")
check_ms = (time.time() - start) * 1000
test(f"/api/check responds in <2000ms ({check_ms:.0f}ms)", check_ms < 2000)


# =========================================================================
print(f"\n{'='*50}")
print(f"⚡ QA Results: {passed} passed, {failed} failed, {warnings} warnings")
print(f"{'='*50}")

if failed > 0:
    print(f"\n🔴 {failed} test(s) FAILED — see details above")
    sys.exit(1)
elif warnings > 0:
    print(f"\n🟡 All tests passed with {warnings} warning(s)")
else:
    print(f"\n🟢 All tests passed!")

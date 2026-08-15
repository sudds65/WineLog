"""API and breakeven-maths tests."""
import io

import pytest

from app.db import DEFAULT_SETTINGS
from conftest import TEST_PASSWORD, TEST_USER

# Breakeven target = membership fee + the tax paid on it.
TARGET = int(DEFAULT_SETTINGS["membership_fee_cents"]) + int(
    DEFAULT_SETTINGS["membership_tax_cents"]
)

PROTECTED = [
    ("get", "/api/stats"),
    ("get", "/api/receipts"),
    ("get", "/api/settings"),
    ("get", "/api/search"),
    ("get", "/api/insights"),
    ("get", "/api/export.csv"),
]


def purchase(client, date, description, amount, category="wine", percent=50):
    return client.post(
        "/api/purchases",
        json={
            "purchased_on": date,
            "description": description,
            "pre_discount": amount,
            "category": category,
            "discount_percent": percent,
        },
    )


# ── auth ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", PROTECTED)
def test_endpoints_require_a_session(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_login_rejects_a_bad_password(client):
    response = client.post("/api/login", json={"username": TEST_USER, "password": "nope"})
    assert response.status_code == 401


def test_login_then_logout(client):
    assert client.post(
        "/api/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    ).status_code == 200
    assert client.get("/api/me").json()["username"] == TEST_USER
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_writes_require_the_app_header(auth_client):
    """SameSite=Lax plus a custom header keeps cross-site posts out."""
    response = auth_client.post(
        "/api/purchases",
        json={"purchased_on": "2026-08-09", "description": "x", "pre_discount": "10"},
        headers={"X-WineLog-App": ""},
    )
    assert response.status_code == 403


# ── breakeven maths ───────────────────────────────────────────────────


def test_stats_start_at_the_membership_price(auth_client):
    """$1,500 fee + $105 tax (7%, per the Obscure receipts) = $1,605 target."""
    stats = auth_client.get("/api/stats").json()
    assert stats["membership_fee_cents"] == 150000
    assert stats["membership_tax_cents"] == 10500
    assert stats["target_cents"] == 160500
    assert stats["saved_cents"] == 0
    assert stats["remaining_cents"] == 160500
    assert stats["broke_even"] is False


def test_default_term_is_twelve_months_from_signup(auth_client):
    stats = auth_client.get("/api/stats").json()
    assert stats["term_start"] == "2026-08-07"
    assert stats["term_end"] == "2027-08-06"


def test_manual_purchase_saves_half_and_moves_the_tally(auth_client):
    response = purchase(auth_client, "2026-08-09", "Two glasses", "31.00")
    assert response.status_code == 201

    stats = response.json()["stats"]
    assert stats["saved_cents"] == 1550
    assert stats["pre_discount_cents"] == 3100
    assert stats["paid_cents"] == 1550
    assert stats["remaining_cents"] == TARGET - 1550


def test_savings_accumulate_across_visits(auth_client):
    purchase(auth_client, "2026-08-07", "Two glasses", "30.00")
    purchase(auth_client, "2026-08-09", "Two glasses", "31.00")
    purchase(auth_client, "2026-08-15", "Rioja", "172.00")
    stats = purchase(auth_client, "2026-08-15", "Beer", "18.00", category="beer").json()["stats"]

    assert stats["saved_cents"] == 1500 + 1550 + 8600 + 900
    assert stats["visit_count"] == 4
    assert stats["remaining_cents"] == TARGET - 12550
    # Cumulative series is ordered oldest first and dated purchases merge by day.
    assert [row["date"] for row in stats["series"]] == ["2026-08-07", "2026-08-09", "2026-08-15"]
    assert stats["series"][-1]["cumulative_cents"] == 12550


def test_breakeven_is_reached_and_clamped(auth_client):
    purchase(auth_client, "2026-08-09", "Cellar raid", "3300.00")
    stats = auth_client.get("/api/stats").json()
    assert stats["saved_cents"] == 165000
    assert stats["saved_cents"] > TARGET
    assert stats["broke_even"] is True
    assert stats["remaining_cents"] == 0
    assert stats["progress_percent"] == 100.0


def test_target_follows_the_settings(auth_client):
    auth_client.put("/api/settings", json={"membership_fee": "1500", "membership_tax": "105"})
    stats = auth_client.get("/api/stats").json()
    assert stats["target_cents"] == 160500


def test_settings_reject_an_inverted_term(auth_client):
    response = auth_client.put(
        "/api/settings", json={"term_start": "2027-01-01", "term_end": "2026-01-01"}
    )
    assert response.status_code == 400


def test_negative_amount_is_rejected(auth_client):
    assert purchase(auth_client, "2026-08-09", "Refund", "-10.00").status_code == 422


# ── receipts ──────────────────────────────────────────────────────────

RECEIPT_BODY = {
    "purchased_on": "2026-08-09",
    "receipt_no": "XzEm",
    "merchant": "Obscure Wine Co. Aurburndale",
    "subtotal_cents": 5350,
    "tax_cents": 374,
    "tip_cents": 1145,
    "total_cents": 6869,
    "items": [
        {
            "description": "3 Cheeses & 2 Meats",
            "category": "other",
            "reg_price_cents": 3800,
            "discount_cents": 0,
            "paid_cents": 3800,
            "qualifying": False,
        },
        {
            "description": "2022 Nebel Riesling",
            "category": "wine",
            "serving": "Glass",
            "reg_price_cents": 1100,
            "discount_cents": 550,
            "paid_cents": 550,
            "qualifying": True,
        },
        {
            "description": "2022 Bava Ruche Monferrato",
            "category": "wine",
            "serving": "Glass",
            "reg_price_cents": 2000,
            "discount_cents": 1000,
            "paid_cents": 1000,
            "qualifying": True,
        },
    ],
}


def test_receipt_counts_only_the_discounted_lines(auth_client):
    stats = auth_client.post("/api/receipts", json=RECEIPT_BODY).json()["stats"]
    assert stats["saved_cents"] == 1550
    assert stats["pre_discount_cents"] == 3100     # the $38 board is excluded
    assert stats["item_count"] == 2
    assert stats["visit_count"] == 1


def test_receipt_keeps_the_food_line_for_context(auth_client):
    receipt_id = auth_client.post("/api/receipts", json=RECEIPT_BODY).json()["id"]
    receipt = auth_client.get(f"/api/receipts/{receipt_id}").json()
    assert len(receipt["items"]) == 3
    assert receipt["saved_cents"] == 1550


def test_same_receipt_number_is_rejected(auth_client):
    assert auth_client.post("/api/receipts", json=RECEIPT_BODY).status_code == 201
    duplicate = auth_client.post("/api/receipts", json=RECEIPT_BODY)
    assert duplicate.status_code == 409
    assert "already" in duplicate.json()["detail"].lower()


def test_deleting_a_receipt_rolls_the_tally_back(auth_client):
    receipt_id = auth_client.post("/api/receipts", json=RECEIPT_BODY).json()["id"]
    stats = auth_client.delete(f"/api/receipts/{receipt_id}").json()["stats"]
    assert stats["saved_cents"] == 0
    assert stats["visit_count"] == 0
    assert auth_client.get(f"/api/receipts/{receipt_id}").status_code == 404


def test_receipt_list_includes_line_items(auth_client):
    auth_client.post("/api/receipts", json=RECEIPT_BODY)
    receipts = auth_client.get("/api/receipts").json()["receipts"]
    assert len(receipts) == 1
    assert len(receipts[0]["items"]) == 3
    assert receipts[0]["saved_cents"] == 1550
    assert receipts[0]["qualifying_count"] == 2


# ── PDF ingest ────────────────────────────────────────────────────────


def test_non_pdf_upload_is_rejected(auth_client):
    response = auth_client.post(
        "/api/receipts/parse",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert response.status_code == 400


def test_pdf_with_wrong_magic_bytes_is_rejected(auth_client):
    response = auth_client.post(
        "/api/receipts/parse",
        files={"file": ("fake.pdf", io.BytesIO(b"not really a pdf"), "application/pdf")},
    )
    assert response.status_code == 400


# ── search and insights ───────────────────────────────────────────────


def test_search_matches_on_words_in_any_order(auth_client):
    auth_client.post("/api/receipts", json=RECEIPT_BODY)
    hits = auth_client.get("/api/search?q=riesling+nebel").json()
    assert [item["description"] for item in hits["items"]] == ["2022 Nebel Riesling"]
    assert hits["summary"]["saved_cents"] == 550


def test_search_excludes_non_counting_lines_by_default(auth_client):
    auth_client.post("/api/receipts", json=RECEIPT_BODY)
    assert auth_client.get("/api/search?q=cheeses").json()["items"] == []
    included = auth_client.get("/api/search?q=cheeses&include_non_counting=true").json()
    assert len(included["items"]) == 1


def test_search_filters_by_date_and_category(auth_client):
    purchase(auth_client, "2026-08-01", "Old pour", "20.00")
    purchase(auth_client, "2026-09-01", "New pour", "40.00", category="beer")

    windowed = auth_client.get("/api/search?date_from=2026-08-20").json()
    assert [i["description"] for i in windowed["items"]] == ["New pour"]

    beer = auth_client.get("/api/search?category=beer").json()
    assert [i["description"] for i in beer["items"]] == ["New pour"]


def test_search_rejects_a_malformed_date(auth_client):
    assert auth_client.get("/api/search?date_from=last-tuesday").status_code == 400


def test_search_sorts_by_price(auth_client):
    purchase(auth_client, "2026-08-01", "Cheap pour", "12.00")
    purchase(auth_client, "2026-08-02", "Big bottle", "172.00")
    hits = auth_client.get("/api/search?sort=price").json()
    assert [i["description"] for i in hits["items"]] == ["Big bottle", "Cheap pour"]


def test_insights_answer_the_usual_questions(auth_client):
    auth_client.post("/api/receipts", json=RECEIPT_BODY)
    purchase(auth_client, "2026-08-15", "2023 Nebel Riesling", "11.00")

    data = auth_client.get("/api/insights?period=all").json()
    assert data["priciest_item"]["description"] == "2022 Bava Ruche Monferrato"
    assert data["priciest_item"]["reg_price_cents"] == 2000
    assert data["biggest_saving_item"]["discount_cents"] == 1000
    assert data["biggest_visit"]["saved_cents"] == 1550

    # Vintages collapse, so the same wine across years reads as a repeat order.
    top = data["most_ordered"][0]
    assert top["times"] == 2
    assert "Riesling" in top["label"]


def test_insights_period_windows_the_data(auth_client):
    purchase(auth_client, "2020-08-01", "Ancient pour", "50.00")
    this_year = auth_client.get("/api/insights?period=year").json()
    assert this_year["totals"]["item_count"] == 0

    all_time = auth_client.get("/api/insights?period=all").json()
    assert all_time["totals"]["item_count"] == 1


# ── export ────────────────────────────────────────────────────────────


def test_csv_export_marks_what_counts(auth_client):
    auth_client.post("/api/receipts", json=RECEIPT_BODY)
    response = auth_client.get("/api/export.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]

    lines = response.text.strip().splitlines()
    assert lines[0].startswith("date,receipt_no")
    assert len(lines) == 4
    assert lines[1].endswith("no")      # the cheese board
    assert lines[2].endswith("yes")     # the Riesling

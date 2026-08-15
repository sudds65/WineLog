"""Reads and writes over the receipt/item tables, plus the breakeven maths.

Every total in here filters on ``items.qualifying = 1`` — the founders-discounted
wine and beer. Food and undiscounted lines are stored for context and never
counted.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from .db import get_settings, transaction
from .receipt_parser import ParsedReceipt, classify
from .search import normalize_description

QUALIFYING = "items.qualifying = 1"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


def list_receipts(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    sql = """
        SELECT r.*,
               COALESCE(q.saved_cents, 0)        AS saved_cents,
               COALESCE(q.pre_discount_cents, 0) AS pre_discount_cents,
               COALESCE(q.qualifying_paid_cents, 0) AS qualifying_paid_cents,
               COALESCE(q.qualifying_count, 0)   AS qualifying_count,
               COALESCE(c.item_count, 0)         AS item_count
          FROM receipts r
          LEFT JOIN (
               SELECT receipt_id,
                      SUM(discount_cents)  AS saved_cents,
                      SUM(reg_price_cents) AS pre_discount_cents,
                      SUM(paid_cents)      AS qualifying_paid_cents,
                      COUNT(*)             AS qualifying_count
                 FROM items WHERE qualifying = 1 GROUP BY receipt_id
          ) q ON q.receipt_id = r.id
          LEFT JOIN (
               SELECT receipt_id, COUNT(*) AS item_count FROM items GROUP BY receipt_id
          ) c ON c.receipt_id = r.id
         ORDER BY r.purchased_on DESC, r.id DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    receipts = [dict(row) for row in conn.execute(sql).fetchall()]
    if not receipts:
        return receipts

    # Attach line items so the log can show each receipt broken out.
    placeholders = ",".join("?" for _ in receipts)
    rows = conn.execute(
        f"""
        SELECT * FROM items WHERE receipt_id IN ({placeholders})
         ORDER BY receipt_id, position, id
        """,
        [r["id"] for r in receipts],
    ).fetchall()

    by_receipt: dict[int, list[dict]] = {}
    for row in rows:
        by_receipt.setdefault(row["receipt_id"], []).append(dict(row))
    for receipt in receipts:
        receipt["items"] = by_receipt.get(receipt["id"], [])
    return receipts


def get_receipt(conn: sqlite3.Connection, receipt_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if row is None:
        return None
    receipt = dict(row)
    receipt["items"] = [
        dict(item)
        for item in conn.execute(
            "SELECT * FROM items WHERE receipt_id = ? ORDER BY position, id",
            (receipt_id,),
        ).fetchall()
    ]
    receipt["saved_cents"] = sum(i["discount_cents"] for i in receipt["items"] if i["qualifying"])
    receipt["pre_discount_cents"] = sum(
        i["reg_price_cents"] for i in receipt["items"] if i["qualifying"]
    )
    return receipt


def totals(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(items.discount_cents), 0)  AS saved_cents,
               COALESCE(SUM(items.reg_price_cents), 0) AS pre_discount_cents,
               COALESCE(SUM(items.paid_cents), 0)      AS paid_cents,
               COUNT(*)                                AS item_count,
               COUNT(DISTINCT items.receipt_id)        AS visit_count
          FROM items WHERE {QUALIFYING}
        """
    ).fetchone()
    return dict(row)


def category_totals(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT items.category,
                   SUM(items.discount_cents)  AS saved_cents,
                   SUM(items.reg_price_cents) AS pre_discount_cents,
                   COUNT(*)                   AS item_count
              FROM items WHERE {QUALIFYING}
             GROUP BY items.category ORDER BY saved_cents DESC
            """
        ).fetchall()
    ]


def savings_series(conn: sqlite3.Connection) -> list[dict]:
    """Cumulative savings by purchase date, oldest first."""
    rows = conn.execute(
        f"""
        SELECT receipts.purchased_on AS day,
               SUM(items.discount_cents) AS saved_cents
          FROM items JOIN receipts ON receipts.id = items.receipt_id
         WHERE {QUALIFYING}
         GROUP BY receipts.purchased_on ORDER BY receipts.purchased_on
        """
    ).fetchall()
    series: list[dict] = []
    running = 0
    for row in rows:
        running += _int(row["saved_cents"])
        series.append(
            {"date": row["day"], "saved_cents": _int(row["saved_cents"]), "cumulative_cents": running}
        )
    return series


def stats(conn: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    settings = get_settings(conn)
    agg = totals(conn)

    fee = _int(settings.get("membership_fee_cents"), 150000)
    tax = _int(settings.get("membership_tax_cents"), 0)
    target = fee + tax
    saved = _int(agg["saved_cents"])
    remaining = max(0, target - saved)

    term_start = _parse_date(settings.get("term_start"))
    term_end = _parse_date(settings.get("term_end"))
    series = savings_series(conn)
    first_purchase = _parse_date(series[0]["date"]) if series else None
    last_purchase = _parse_date(series[-1]["date"]) if series else None

    visits = _int(agg["visit_count"])
    start = term_start or first_purchase
    days_elapsed = max(1, (today - start).days + 1) if start else 1
    daily_rate = saved / days_elapsed if saved else 0.0

    projected_breakeven: str | None = None
    days_to_breakeven: int | None = None
    if remaining == 0:
        projected_breakeven = last_purchase.isoformat() if last_purchase else None
        days_to_breakeven = 0
    elif daily_rate > 0:
        days_to_breakeven = int(remaining / daily_rate) + 1
        # Cap the projection so a very slow start cannot overflow the date type.
        if days_to_breakeven < 365 * 50:
            projected_breakeven = (today + timedelta(days=days_to_breakeven)).isoformat()

    days_left = (term_end - today).days if term_end else None
    required_daily = (remaining / days_left) if days_left and days_left > 0 and remaining else None
    on_pace: bool | None = None
    if remaining == 0:
        on_pace = True
    elif required_daily is not None:
        on_pace = daily_rate >= required_daily
    elif days_left is not None and days_left <= 0:
        on_pace = False

    return {
        "target_cents": target,
        "membership_fee_cents": fee,
        "membership_tax_cents": tax,
        "saved_cents": saved,
        "remaining_cents": remaining,
        "pre_discount_cents": _int(agg["pre_discount_cents"]),
        "paid_cents": _int(agg["paid_cents"]),
        "progress_percent": round(min(100.0, saved / target * 100), 1) if target else 0.0,
        "item_count": _int(agg["item_count"]),
        "visit_count": visits,
        "avg_saved_per_visit_cents": round(saved / visits) if visits else 0,
        "first_purchase": first_purchase.isoformat() if first_purchase else None,
        "last_purchase": last_purchase.isoformat() if last_purchase else None,
        "term_start": term_start.isoformat() if term_start else None,
        "term_end": term_end.isoformat() if term_end else None,
        "days_elapsed": days_elapsed,
        "days_left": days_left,
        "daily_rate_cents": round(daily_rate),
        "required_daily_cents": round(required_daily) if required_daily else None,
        "projected_breakeven": projected_breakeven,
        "days_to_breakeven": days_to_breakeven,
        "on_pace": on_pace,
        "broke_even": remaining == 0,
        "series": series,
        "categories": category_totals(conn),
        "today": today.isoformat(),
    }


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def _insert_items(conn: sqlite3.Connection, receipt_id: int, items: list[dict]) -> None:
    for position, item in enumerate(items):
        conn.execute(
            """
            INSERT INTO items (receipt_id, position, description, description_norm,
                               detail, category, serving, reg_price_cents,
                               discount_cents, paid_cents, qualifying)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                position,
                item["description"],
                normalize_description(item["description"]),
                item.get("detail"),
                item.get("category") or "other",
                item.get("serving"),
                _int(item.get("reg_price_cents")),
                _int(item.get("discount_cents")),
                _int(item.get("paid_cents")),
                1 if item.get("qualifying") else 0,
            ),
        )


def save_receipt(
    conn: sqlite3.Connection,
    *,
    purchased_on: str,
    items: list[dict],
    source: str,
    user_id: int | None = None,
    receipt_no: str | None = None,
    purchased_at: str | None = None,
    merchant: str | None = None,
    subtotal_cents: int | None = None,
    tax_cents: int | None = None,
    tip_cents: int | None = None,
    total_cents: int | None = None,
    note: str | None = None,
    filename: str | None = None,
    file_sha256: str | None = None,
) -> int:
    from datetime import datetime, timezone

    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO receipts (receipt_no, purchased_on, purchased_at, source,
                                  merchant, subtotal_cents, tax_cents, tip_cents,
                                  total_cents, note, filename, file_sha256,
                                  created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_no or None,
                purchased_on,
                purchased_at,
                source,
                merchant,
                subtotal_cents,
                tax_cents,
                tip_cents,
                total_cents,
                note,
                filename,
                file_sha256,
                datetime.now(timezone.utc).isoformat(),
                user_id,
            ),
        )
        receipt_id = int(cursor.lastrowid)
        _insert_items(conn, receipt_id, items)
    return receipt_id


def save_manual_purchase(
    conn: sqlite3.Connection,
    *,
    purchased_on: str,
    description: str,
    pre_discount_cents: int,
    discount_percent: float,
    category: str | None = None,
    note: str | None = None,
    user_id: int | None = None,
) -> int:
    """A manual entry is one qualifying line: pre-discount price and the rate."""
    discount_cents = round(pre_discount_cents * discount_percent / 100)
    item = {
        "description": description,
        "category": category or classify(description),
        "reg_price_cents": pre_discount_cents,
        "discount_cents": discount_cents,
        "paid_cents": pre_discount_cents - discount_cents,
        "qualifying": True,
    }
    return save_receipt(
        conn,
        purchased_on=purchased_on,
        items=[item],
        source="manual",
        user_id=user_id,
        note=note,
        subtotal_cents=item["paid_cents"],
        total_cents=item["paid_cents"],
    )


def receipt_from_parsed(parsed: ParsedReceipt) -> dict:
    """Shape a ParsedReceipt into the JSON the review screen edits."""
    return {
        "receipt_no": parsed.receipt_no,
        "purchased_on": parsed.purchased_on.isoformat() if parsed.purchased_on else None,
        "purchased_at": parsed.purchased_at,
        "merchant": parsed.merchant,
        "subtotal_cents": parsed.subtotal_cents,
        "tax_cents": parsed.tax_cents,
        "tip_cents": parsed.tip_cents,
        "total_cents": parsed.total_cents,
        "savings_cents": parsed.savings_cents,
        "pre_discount_cents": parsed.pre_discount_cents,
        "warnings": parsed.warnings,
        "items": [
            {
                "description": i.description,
                "detail": i.detail,
                "category": i.category,
                "serving": i.serving,
                "reg_price_cents": i.reg_price_cents,
                "discount_cents": i.discount_cents,
                "paid_cents": i.paid_cents,
                "qualifying": i.qualifying,
                "discount_percent": i.discount_percent,
            }
            for i in parsed.items
        ],
    }


def delete_receipt(conn: sqlite3.Connection, receipt_id: int) -> bool:
    with transaction(conn):
        cursor = conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    return cursor.rowcount > 0


def find_duplicate(
    conn: sqlite3.Connection, receipt_no: str | None, file_sha256: str | None
) -> dict | None:
    if receipt_no:
        row = conn.execute(
            "SELECT * FROM receipts WHERE receipt_no = ?", (receipt_no,)
        ).fetchone()
        if row:
            return dict(row)
    if file_sha256:
        row = conn.execute(
            "SELECT * FROM receipts WHERE file_sha256 = ?", (file_sha256,)
        ).fetchone()
        if row:
            return dict(row)
    return None

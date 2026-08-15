"""Search and insights over logged purchases.

Answers the questions you actually ask out loud:
  - what's the most we spent on a bottle this year?
  - what's the most we saved in one go?
  - what do we order the most?
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Any

from .db import get_settings

# A leading vintage ("2022 Nebel Riesling") is stripped so the same wine from
# different years groups together in "ordered most often".
RE_VINTAGE = re.compile(r"^(?:19|20)\d{2}\s+")
RE_NOISE = re.compile(r"[^a-z0-9&+ ]+")

SORTS = {
    "date": "r.purchased_on DESC, i.id DESC",
    "saved": "i.discount_cents DESC, r.purchased_on DESC",
    "price": "i.reg_price_cents DESC, r.purchased_on DESC",
    "name": "i.description_norm ASC, r.purchased_on DESC",
}

PERIODS = ("year", "term", "all")


def normalize_description(description: str) -> str:
    text = (description or "").strip().lower()
    text = RE_VINTAGE.sub("", text)
    text = RE_NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def period_bounds(
    conn: sqlite3.Connection, period: str, today: date | None = None
) -> tuple[str | None, str | None, str]:
    """Return (start, end, label) as ISO dates for a named period."""
    today = today or date.today()
    if period == "year":
        return f"{today.year}-01-01", f"{today.year}-12-31", str(today.year)
    if period == "term":
        settings = get_settings(conn)
        start = settings.get("term_start")
        end = settings.get("term_end")
        return start, end, "this membership"
    return None, None, "all time"


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def search_items(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    counting_only: bool = True,
    sort: str = "date",
    limit: int = 200,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []

    if counting_only:
        where.append("i.qualifying = 1")
    if category in ("wine", "beer", "other"):
        where.append("i.category = ?")
        params.append(category)
    if date_from:
        where.append("r.purchased_on >= ?")
        params.append(date_from)
    if date_to:
        where.append("r.purchased_on <= ?")
        params.append(date_to)

    # Every whitespace-separated word must appear somewhere in the item text.
    for term in normalize_description(query).split():
        where.append(
            "(i.description_norm LIKE ? ESCAPE '\\' OR LOWER(COALESCE(i.detail, '')) "
            "LIKE ? ESCAPE '\\' OR LOWER(COALESCE(r.note, '')) LIKE ? ESCAPE '\\')"
        )
        like = f"%{_escape_like(term)}%"
        params.extend([like, like, like])

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = SORTS.get(sort, SORTS["date"])
    limit = max(1, min(int(limit), 1000))

    rows = conn.execute(
        f"""
        SELECT i.id, i.description, i.category, i.serving, i.detail,
               i.reg_price_cents, i.discount_cents, i.paid_cents, i.qualifying,
               r.id AS receipt_id, r.purchased_on, r.receipt_no, r.source, r.note
          FROM items i JOIN receipts r ON r.id = i.receipt_id
          {clause}
         ORDER BY {order}
         LIMIT {limit}
        """,
        params,
    ).fetchall()

    summary = conn.execute(
        f"""
        SELECT COUNT(*) AS item_count,
               COALESCE(SUM(i.discount_cents), 0)  AS saved_cents,
               COALESCE(SUM(i.reg_price_cents), 0) AS pre_discount_cents,
               COALESCE(SUM(i.paid_cents), 0)      AS paid_cents
          FROM items i JOIN receipts r ON r.id = i.receipt_id
          {clause}
        """,
        params,
    ).fetchone()

    return {
        "items": [dict(row) for row in rows],
        "summary": dict(summary),
        "truncated": len(rows) >= limit,
    }


# --------------------------------------------------------------------------
# insights
# --------------------------------------------------------------------------


def insights(
    conn: sqlite3.Connection, period: str = "year", today: date | None = None
) -> dict[str, Any]:
    period = period if period in PERIODS else "year"
    start, end, label = period_bounds(conn, period, today)

    where = ["i.qualifying = 1"]
    params: list[Any] = []
    if start:
        where.append("r.purchased_on >= ?")
        params.append(start)
    if end:
        where.append("r.purchased_on <= ?")
        params.append(end)
    clause = "WHERE " + " AND ".join(where)

    def item_extreme(order_by: str) -> dict | None:
        row = conn.execute(
            f"""
            SELECT i.description, i.category, i.serving, i.reg_price_cents,
                   i.discount_cents, i.paid_cents, r.purchased_on, r.id AS receipt_id
              FROM items i JOIN receipts r ON r.id = i.receipt_id
              {clause}
             ORDER BY {order_by} LIMIT 1
            """,
            params,
        ).fetchone()
        return dict(row) if row else None

    biggest_visit = conn.execute(
        f"""
        SELECT r.id AS receipt_id, r.purchased_on, r.receipt_no,
               SUM(i.discount_cents)  AS saved_cents,
               SUM(i.reg_price_cents) AS pre_discount_cents,
               COUNT(*)               AS item_count
          FROM items i JOIN receipts r ON r.id = i.receipt_id
          {clause}
         GROUP BY r.id ORDER BY saved_cents DESC LIMIT 1
        """,
        params,
    ).fetchone()

    most_ordered = conn.execute(
        f"""
        SELECT i.description_norm,
               MAX(i.description)         AS label,
               COUNT(*)                   AS times,
               SUM(i.discount_cents)      AS saved_cents,
               SUM(i.reg_price_cents)     AS pre_discount_cents,
               MAX(r.purchased_on)        AS last_ordered,
               i.category
          FROM items i JOIN receipts r ON r.id = i.receipt_id
          {clause}
         GROUP BY i.description_norm
         ORDER BY times DESC, saved_cents DESC LIMIT 8
        """,
        params,
    ).fetchall()

    busiest_month = conn.execute(
        f"""
        SELECT substr(r.purchased_on, 1, 7) AS month,
               SUM(i.discount_cents) AS saved_cents,
               COUNT(DISTINCT r.id)  AS visit_count
          FROM items i JOIN receipts r ON r.id = i.receipt_id
          {clause}
         GROUP BY month ORDER BY saved_cents DESC LIMIT 1
        """,
        params,
    ).fetchone()

    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS item_count,
               COUNT(DISTINCT r.id) AS visit_count,
               COALESCE(SUM(i.discount_cents), 0)  AS saved_cents,
               COALESCE(SUM(i.reg_price_cents), 0) AS pre_discount_cents,
               COALESCE(SUM(i.paid_cents), 0)      AS paid_cents
          FROM items i JOIN receipts r ON r.id = i.receipt_id
          {clause}
        """,
        params,
    ).fetchone()

    return {
        "period": period,
        "label": label,
        "start": start,
        "end": end,
        "totals": dict(totals),
        "priciest_item": item_extreme("i.reg_price_cents DESC"),
        "biggest_saving_item": item_extreme("i.discount_cents DESC"),
        "biggest_visit": dict(biggest_visit) if biggest_visit else None,
        "busiest_month": dict(busiest_month) if busiest_month else None,
        "most_ordered": [dict(row) for row in most_ordered],
    }

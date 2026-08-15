"""WineLog — founders-membership breakeven tracker for Obscure Wine Co."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import auth, config, db, service
from .auth import AuthedUser, CsrfGuard, CurrentUser
from .receipt_parser import ReceiptParseError, parse_pdf, to_cents
from .search import insights, search_items

CATEGORIES = ("wine", "beer", "other")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    _prune_orphan_uploads()
    yield


app = FastAPI(
    title="WineLog", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=10, max_length=512)


class MoneyMixin(BaseModel):
    @staticmethod
    def _money_to_cents(value: Any, field_name: str) -> int:
        try:
            cents = to_cents(str(value))
        except Exception as exc:
            raise ValueError(f"{field_name} must be an amount like 31.00") from exc
        if cents < 0:
            raise ValueError(f"{field_name} cannot be negative")
        if cents > 100_000_00:
            raise ValueError(f"{field_name} looks too large")
        return cents


class ManualPurchaseIn(MoneyMixin):
    purchased_on: date
    description: str = Field(min_length=1, max_length=200)
    pre_discount: str = Field(description="Full menu price before the member discount")
    discount_percent: float = Field(default=50.0, ge=0, le=100)
    category: Literal["wine", "beer"] = "wine"
    note: str | None = Field(default=None, max_length=500)

    @field_validator("pre_discount")
    @classmethod
    def _check_amount(cls, value: str) -> str:
        MoneyMixin._money_to_cents(value, "pre_discount")
        return value

    @property
    def pre_discount_cents(self) -> int:
        return MoneyMixin._money_to_cents(self.pre_discount, "pre_discount")


class ItemIn(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    detail: str | None = Field(default=None, max_length=1000)
    category: str = "other"
    serving: str | None = Field(default=None, max_length=60)
    reg_price_cents: int = Field(ge=0, le=100_000_00)
    discount_cents: int = Field(ge=0, le=100_000_00)
    paid_cents: int = Field(ge=0, le=100_000_00)
    qualifying: bool = False

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: str) -> str:
        return value if value in CATEGORIES else "other"


class ReceiptIn(BaseModel):
    purchased_on: date
    items: list[ItemIn] = Field(min_length=1, max_length=200)
    receipt_no: str | None = Field(default=None, max_length=64)
    purchased_at: str | None = Field(default=None, max_length=40)
    merchant: str | None = Field(default=None, max_length=200)
    subtotal_cents: int | None = Field(default=None, ge=0)
    tax_cents: int | None = Field(default=None, ge=0)
    tip_cents: int | None = Field(default=None, ge=0)
    total_cents: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)
    filename: str | None = Field(default=None, max_length=255)
    file_sha256: str | None = Field(default=None, max_length=64)


class SettingsIn(MoneyMixin):
    membership_fee: str | None = None
    membership_tax: str | None = None
    term_start: date | None = None
    term_end: date | None = None
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    member_name: str | None = Field(default=None, max_length=120)


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def _prune_orphan_uploads() -> None:
    """Drop staged PDFs whose upload was never confirmed."""
    if not config.UPLOAD_DIR.exists():
        return
    with db.get_conn() as conn:
        known = {
            row["file_sha256"]
            for row in conn.execute(
                "SELECT file_sha256 FROM receipts WHERE file_sha256 IS NOT NULL"
            ).fetchall()
        }
    cutoff = datetime.now(timezone.utc).timestamp() - 86_400
    for path in config.UPLOAD_DIR.glob("*.pdf"):
        if path.stem not in known and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# auth routes
# --------------------------------------------------------------------------


@app.post("/api/login")
def login(payload: LoginIn, response: Response, _: None = CsrfGuard) -> dict:
    locked = auth.is_locked_out(payload.username)
    if locked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed attempts. Try again in {locked // 60 + 1} minute(s).",
        )
    with db.get_conn() as conn:
        user = auth.authenticate(conn, payload.username, payload.password)
        if user is None:
            auth.record_failure(payload.username)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
        auth.clear_failures(payload.username)
        token, expires = auth.start_session(conn, user["id"])

    response.set_cookie(
        config.COOKIE_NAME,
        token,
        max_age=config.SESSION_DAYS * 86_400,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
    )
    return {"username": user["username"], "expires_at": expires.isoformat()}


@app.post("/api/logout")
def logout(response: Response, user: CurrentUser = AuthedUser, _: None = CsrfGuard) -> dict:
    with db.get_conn() as conn:
        auth.end_session(conn, user.token)
    response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: CurrentUser = AuthedUser) -> dict:
    return {"username": user.username}


@app.post("/api/password")
def change_password(
    payload: PasswordIn,
    response: Response,
    user: CurrentUser = AuthedUser,
    _: None = CsrfGuard,
) -> dict:
    with db.get_conn() as conn:
        if auth.authenticate(conn, user.username, payload.current_password) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
        auth.set_password(conn, user.id, payload.new_password)
    response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"ok": True, "message": "Password changed. Sign in again on each device."}


# --------------------------------------------------------------------------
# data routes
# --------------------------------------------------------------------------


@app.get("/api/stats")
def get_stats(user: CurrentUser = AuthedUser) -> dict:
    with db.get_conn() as conn:
        return service.stats(conn)


@app.get("/api/receipts")
def get_receipts(user: CurrentUser = AuthedUser, limit: int | None = None) -> dict:
    with db.get_conn() as conn:
        return {"receipts": service.list_receipts(conn, limit)}


@app.get("/api/receipts/{receipt_id}")
def get_one_receipt(receipt_id: int, user: CurrentUser = AuthedUser) -> dict:
    with db.get_conn() as conn:
        receipt = service.get_receipt(conn, receipt_id)
    if receipt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found")
    return receipt


@app.post("/api/purchases", status_code=status.HTTP_201_CREATED)
def add_manual_purchase(
    payload: ManualPurchaseIn, user: CurrentUser = AuthedUser, _: None = CsrfGuard
) -> dict:
    with db.get_conn() as conn:
        receipt_id = service.save_manual_purchase(
            conn,
            purchased_on=payload.purchased_on.isoformat(),
            description=payload.description.strip(),
            pre_discount_cents=payload.pre_discount_cents,
            discount_percent=payload.discount_percent,
            category=payload.category,
            note=payload.note,
            user_id=user.id,
        )
        return {"id": receipt_id, "stats": service.stats(conn)}


@app.post("/api/receipts", status_code=status.HTTP_201_CREATED)
def add_receipt(
    payload: ReceiptIn, user: CurrentUser = AuthedUser, _: None = CsrfGuard
) -> dict:
    if not any(item.qualifying for item in payload.items):
        # Allowed, but the caller should know it moves the needle by $0.
        pass
    receipt_no = (payload.receipt_no or "").strip() or None
    sha = (payload.file_sha256 or "").strip() or None
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid file reference")

    with db.get_conn() as conn:
        duplicate = service.find_duplicate(conn, receipt_no, sha)
        if duplicate:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Already logged on {duplicate['purchased_on']}"
                + (f" (receipt #{duplicate['receipt_no']})" if duplicate["receipt_no"] else ""),
            )
        try:
            receipt_id = service.save_receipt(
                conn,
                purchased_on=payload.purchased_on.isoformat(),
                items=[item.model_dump() for item in payload.items],
                source="pdf" if sha else "manual",
                user_id=user.id,
                receipt_no=receipt_no,
                purchased_at=payload.purchased_at,
                merchant=payload.merchant,
                subtotal_cents=payload.subtotal_cents,
                tax_cents=payload.tax_cents,
                tip_cents=payload.tip_cents,
                total_cents=payload.total_cents,
                note=payload.note,
                filename=payload.filename,
                file_sha256=sha,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status.HTTP_409_CONFLICT, "This receipt is already logged")
        return {"id": receipt_id, "stats": service.stats(conn)}


@app.delete("/api/receipts/{receipt_id}")
def remove_receipt(
    receipt_id: int, user: CurrentUser = AuthedUser, _: None = CsrfGuard
) -> dict:
    with db.get_conn() as conn:
        if not service.delete_receipt(conn, receipt_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found")
        return {"ok": True, "stats": service.stats(conn)}


@app.post("/api/receipts/parse")
async def parse_receipt(
    file: Annotated[UploadFile, File()],
    user: CurrentUser = AuthedUser,
    _: None = CsrfGuard,
) -> dict:
    filename = (file.filename or "receipt.pdf").strip()
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please upload a PDF receipt")

    payload = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(payload) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"PDF is larger than {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    if not payload.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That file is not a PDF")

    sha = hashlib.sha256(payload).hexdigest()
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    staged = config.UPLOAD_DIR / f"{sha}.pdf"
    staged.write_bytes(payload)

    try:
        parsed = parse_pdf(staged)
    except ReceiptParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    result = service.receipt_from_parsed(parsed)
    result["file_sha256"] = sha
    result["filename"] = filename

    with db.get_conn() as conn:
        duplicate = service.find_duplicate(conn, parsed.receipt_no, sha)
    result["duplicate_of"] = (
        {
            "id": duplicate["id"],
            "purchased_on": duplicate["purchased_on"],
            "receipt_no": duplicate["receipt_no"],
        }
        if duplicate
        else None
    )
    return result


# --------------------------------------------------------------------------
# search and insights
# --------------------------------------------------------------------------


@app.get("/api/search")
def search(
    user: CurrentUser = AuthedUser,
    q: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    include_non_counting: bool = False,
    sort: str = "date",
    limit: int = 200,
) -> dict:
    for value, name in ((date_from, "date_from"), (date_to, "date_to")):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} must be YYYY-MM-DD")
    with db.get_conn() as conn:
        return search_items(
            conn,
            query=q[:200],
            date_from=date_from,
            date_to=date_to,
            category=category,
            counting_only=not include_non_counting,
            sort=sort,
            limit=limit,
        )


@app.get("/api/insights")
def get_insights(user: CurrentUser = AuthedUser, period: str = "year") -> dict:
    with db.get_conn() as conn:
        return insights(conn, period)


# --------------------------------------------------------------------------
# settings and export
# --------------------------------------------------------------------------


@app.get("/api/settings")
def read_settings(user: CurrentUser = AuthedUser) -> dict:
    with db.get_conn() as conn:
        return db.get_settings(conn)


@app.put("/api/settings")
def update_settings(
    payload: SettingsIn, user: CurrentUser = AuthedUser, _: None = CsrfGuard
) -> dict:
    values: dict[str, str] = {}
    if payload.membership_fee is not None:
        values["membership_fee_cents"] = str(
            MoneyMixin._money_to_cents(payload.membership_fee, "membership_fee")
        )
    if payload.membership_tax is not None:
        values["membership_tax_cents"] = str(
            MoneyMixin._money_to_cents(payload.membership_tax, "membership_tax")
        )
    if payload.term_start is not None:
        values["term_start"] = payload.term_start.isoformat()
    if payload.term_end is not None:
        values["term_end"] = payload.term_end.isoformat()
    if payload.discount_percent is not None:
        values["discount_percent"] = str(payload.discount_percent)
    if payload.member_name is not None:
        values["member_name"] = payload.member_name.strip()

    if values.get("term_start") and values.get("term_end"):
        if values["term_end"] <= values["term_start"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Membership end date must be after the start date"
            )

    with db.get_conn() as conn:
        with db.transaction(conn):
            db.set_settings(conn, values)
        return {"settings": db.get_settings(conn), "stats": service.stats(conn)}


@app.get("/api/export.csv")
def export_csv(user: CurrentUser = AuthedUser) -> StreamingResponse:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.purchased_on, r.receipt_no, r.source, r.merchant,
                   i.description, i.category, i.serving, i.reg_price_cents,
                   i.discount_cents, i.paid_cents, i.qualifying
              FROM items i JOIN receipts r ON r.id = i.receipt_id
             ORDER BY r.purchased_on, r.id, i.position
            """
        ).fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "date", "receipt_no", "source", "merchant", "description", "category",
            "serving", "pre_discount", "saved", "paid", "counts_toward_breakeven",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["purchased_on"], row["receipt_no"] or "", row["source"],
                row["merchant"] or "", row["description"], row["category"],
                row["serving"] or "", f"{row['reg_price_cents'] / 100:.2f}",
                f"{row['discount_cents'] / 100:.2f}", f"{row['paid_cents'] / 100:.2f}",
                "yes" if row["qualifying"] else "no",
            ]
        )
    buffer.seek(0)
    stamp = date.today().isoformat()
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="winelog-{stamp}.csv"'},
    )


# --------------------------------------------------------------------------
# static frontend
# --------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.exception_handler(404)
async def not_found(request, exc) -> JSONResponse | FileResponse:
    if request.url.path.startswith(("/api/", "/static/")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

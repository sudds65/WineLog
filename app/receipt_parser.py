"""Parser for Square receipts (as emailed by Obscure Wine Co. and printed to PDF).

Only line items carrying the founders discount marker count toward breakeven:

    Discount: founders (50%) (-$5.50)

Everything else on the ticket (food boards, undiscounted extras, tip, tax) is
parsed and returned for context, but flagged ``qualifying = False`` so it never
reaches a savings total.

A receipt looks like this once the PDF text is flattened::

    $68.69 #XzEm
    Aug 9 2026 at 2:14 PM
    3 Cheeses & 2 Meats $38.00
    bresaola
    ...
    2022 Nebel Riesling $5.50
    Grmany, Rheinhessen
    Glass
    Reg Price$11.00
    Discount: founders (50%) (-$5.50)
    Purchase Subtotal $53.50
    Sales Tax (7%) $3.74
    Tip $11.45
    Total $68.69
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------

MONEY = r"-?\$\s?([\d,]+\.\d{2})"

# "Discount: founders (50%) (-$5.50)" — the marker that makes a line count.
RE_FOUNDERS = re.compile(
    r"discount:\s*founders\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\)\s*\(\s*-?\s*\$\s?([\d,]+\.\d{2})\s*\)",
    re.IGNORECASE,
)
# Any other discount line, so a non-founders promo is not mistaken for one.
RE_ANY_DISCOUNT = re.compile(
    r"discount:\s*(.+?)\s*\(\s*-?\s*\$\s?([\d,]+\.\d{2})\s*\)", re.IGNORECASE
)
RE_REG_PRICE = re.compile(r"reg(?:ular)?\s*price\s*" + MONEY, re.IGNORECASE)

# "3 Cheeses & 2 Meats $38.00" — description then trailing amount.
RE_LINE_ITEM = re.compile(r"^(?P<desc>.+?)\s*-?\$\s?(?P<amount>[\d,]+\.\d{2})$")
RE_QTY_PREFIX = re.compile(r"^(?P<qty>\d{1,3})\s*(?:x|×)\s*(?P<rest>.+)$", re.IGNORECASE)

RE_RECEIPT_NO = re.compile(r"#([A-Za-z0-9][A-Za-z0-9_-]{2,})")
RE_DATETIME = re.compile(
    r"^(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<day>\d{1,2}),?\s+(?P<year>\d{4})\s+at\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)$"
)

RE_SUBTOTAL = re.compile(r"^(?:purchase\s+)?subtotal\b.*?" + MONEY + r"$", re.IGNORECASE)
RE_TAX = re.compile(r"^(?:sales\s+)?tax\b.*?" + MONEY + r"$", re.IGNORECASE)
RE_TIP = re.compile(r"^(?:tip|gratuity)\b.*?" + MONEY + r"$", re.IGNORECASE)
RE_TOTAL = re.compile(r"^total\b.*?" + MONEY + r"$", re.IGNORECASE)

# Gmail's print chrome, dropped before parsing.
NOISE_PATTERNS = (
    re.compile(r"^https?://", re.IGNORECASE),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},\s+\d{1,2}:\d{2}\s*[AaPp][Mm]\s+Gmail\b"),
    re.compile(r"^\d+/\d+$"),  # page counter
)

SERVING_WORDS = {"glass", "bottle", "can", "draft", "pour", "flight", "half bottle", "carafe"}

BEER_HINTS = (
    "beer", "ale", "ipa", "lager", "pilsner", "pils", "stout", "porter", "saison",
    "hefeweizen", "weiss", "witbier", "gose", "kolsch", "kölsch", "bock", "draft",
    "brewing", "brewery", "brasserie", "trappist", "dubbel", "tripel", "quad",
)
WINE_HINTS = (
    "wine", "vino", "riesling", "cabernet", "merlot", "pinot", "chardonnay",
    "sauvignon", "syrah", "shiraz", "malbec", "tempranillo", "rioja", "sangiovese",
    "chianti", "nebbiolo", "barolo", "barbera", "ruche", "grenache", "garnacha",
    "zinfandel", "rose", "rosé", "champagne", "prosecco", "cava", "sparkling",
    "gruner", "grüner", "albarino", "albariño", "vermentino", "viognier", "gamay",
    "beaujolais", "bordeaux", "burgundy", "montepulciano", "primitivo", "verdejo",
    "muscadet", "chenin", "sancerre", "port", "sherry", "madeira", "red", "white",
)


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------


@dataclass
class ParsedItem:
    description: str
    reg_price_cents: int
    discount_cents: int
    paid_cents: int
    qualifying: bool
    category: str = "other"
    serving: str | None = None
    detail: str | None = None
    discount_percent: float | None = None


@dataclass
class ParsedReceipt:
    receipt_no: str | None = None
    purchased_on: date | None = None
    purchased_at: str | None = None
    merchant: str | None = None
    subtotal_cents: int | None = None
    tax_cents: int | None = None
    tip_cents: int | None = None
    total_cents: int | None = None
    items: list[ParsedItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def qualifying_items(self) -> list[ParsedItem]:
        return [i for i in self.items if i.qualifying]

    @property
    def savings_cents(self) -> int:
        return sum(i.discount_cents for i in self.qualifying_items)

    @property
    def pre_discount_cents(self) -> int:
        return sum(i.reg_price_cents for i in self.qualifying_items)

    @property
    def qualifying_paid_cents(self) -> int:
        return sum(i.paid_cents for i in self.qualifying_items)


class ReceiptParseError(ValueError):
    """Raised when a file cannot be read as a receipt at all."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def to_cents(amount: str | Decimal | float) -> int:
    """Convert a money string like '1,234.50' to integer cents."""
    if isinstance(amount, str):
        cleaned = amount.replace(",", "").replace("$", "").strip()
        try:
            value = Decimal(cleaned)
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise ReceiptParseError(f"unreadable amount: {amount!r}") from exc
    else:
        value = Decimal(str(amount))
    return int((value * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def classify(description: str, detail: str | None = None) -> str:
    """Best-effort wine/beer split. Only ever applied to discounted items."""
    haystack = f"{description} {detail or ''}".lower()
    if any(hint in haystack for hint in BEER_HINTS):
        return "beer"
    if any(hint in haystack for hint in WINE_HINTS):
        return "wine"
    # Founders discount applies to wine and beer only, so an unrecognised
    # discounted pour is far more likely wine than anything else.
    return "wine"


def _clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.replace("\xa0", " ").strip()
        if not line:
            continue
        if any(p.search(line) for p in NOISE_PATTERNS):
            continue
        lines.append(line)
    return lines


def _find_serving(detail_lines: Iterable[str]) -> str | None:
    for line in detail_lines:
        if line.strip().lower() in SERVING_WORDS:
            return line.strip().title()
    return None


def _pick_merchant(candidates: Iterable[str]) -> str | None:
    """Prefer the bare storefront name over the email subject line."""
    best: str | None = None
    for raw in candidates:
        name = re.sub(r"^\s*(?:receipt|order)\s+from\s+", "", raw, flags=re.IGNORECASE)
        name = RE_RECEIPT_NO.sub("", name).strip(" -–—,")
        # Skip the "Let <merchant> know how your experience was" style prompts
        # and any line that still carries sentence noise around the name.
        if not name or len(name) > 60 or re.search(r"\b(know|reply|via)\b", name, re.I):
            continue
        if best is None or len(name) < len(best):
            best = name
    return best


def _is_summary_line(line: str) -> bool:
    return bool(
        RE_SUBTOTAL.match(line)
        or RE_TAX.match(line)
        or RE_TIP.match(line)
        or RE_TOTAL.match(line)
    )


# --------------------------------------------------------------------------
# text extraction
# --------------------------------------------------------------------------


def extract_text(pdf_path: str | Path) -> str:
    """Flatten a PDF to text. Pages are joined so items may span a page break."""
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ReceiptParseError(
            "pdfplumber is not installed; run pip install -r requirements.txt"
        ) from exc

    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except ReceiptParseError:
        raise
    except Exception as exc:
        raise ReceiptParseError(f"could not read PDF: {exc}") from exc

    text = "\n".join(pages)
    if not text.strip():
        raise ReceiptParseError(
            "No text found in the PDF. If this is a photo or scan, the receipt "
            "needs OCR first — or add the purchase manually."
        )
    return text


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_text(text: str) -> ParsedReceipt:
    lines = _clean_lines(text)
    if not lines:
        raise ReceiptParseError("receipt appears to be empty")

    receipt = ParsedReceipt()

    # --- header: receipt number and purchase timestamp -------------------
    merchant_candidates: list[str] = []
    for line in lines:
        if receipt.receipt_no is None:
            match = RE_RECEIPT_NO.search(line)
            if match:
                receipt.receipt_no = match.group(1)
        if "wine co" in line.lower() or "obscure" in line.lower():
            merchant_candidates.append(line)
    receipt.merchant = _pick_merchant(merchant_candidates)

    dt_index = None
    for index, line in enumerate(lines):
        match = RE_DATETIME.match(line)
        if match:
            dt_index = index
            stamp = (
                f"{match.group('mon')} {match.group('day')} {match.group('year')} "
                f"{match.group('time').upper().replace('.', '').replace(' ', '')}"
            )
            try:
                parsed = datetime.strptime(stamp, "%b %d %Y %I:%M%p")
            except ValueError:
                receipt.warnings.append(f"Could not read the date line: {line!r}")
            else:
                receipt.purchased_on = parsed.date()
                receipt.purchased_at = parsed.isoformat(timespec="minutes")
            break

    # --- summary totals ---------------------------------------------------
    end_index = len(lines)
    for index, line in enumerate(lines):
        if RE_SUBTOTAL.match(line) and receipt.subtotal_cents is None:
            receipt.subtotal_cents = to_cents(RE_SUBTOTAL.match(line).group(1))
            end_index = min(end_index, index)
        elif RE_TAX.match(line) and receipt.tax_cents is None:
            receipt.tax_cents = to_cents(RE_TAX.match(line).group(1))
            end_index = min(end_index, index)
        elif RE_TIP.match(line) and receipt.tip_cents is None:
            receipt.tip_cents = to_cents(RE_TIP.match(line).group(1))
            end_index = min(end_index, index)
        elif RE_TOTAL.match(line) and receipt.total_cents is None:
            receipt.total_cents = to_cents(RE_TOTAL.match(line).group(1))

    if receipt.total_cents is None:
        # Square puts the amount beside the receipt number: "$68.69 #XzEm"
        for line in lines[: (dt_index or 0) + 1]:
            if "#" in line:
                money = re.search(MONEY, line)
                if money:
                    receipt.total_cents = to_cents(money.group(1))
                    break

    # --- line items -------------------------------------------------------
    start_index = (dt_index + 1) if dt_index is not None else 0
    body = lines[start_index:end_index]
    receipt.items = _parse_items(body, receipt.warnings)

    if not receipt.items:
        receipt.warnings.append("No line items were found on this receipt.")
    elif not receipt.qualifying_items:
        receipt.warnings.append(
            "No 'Discount: founders (50%)' lines found, so nothing on this "
            "receipt counts toward breakeven."
        )

    _check_consistency(receipt)
    return receipt


def _parse_items(body: list[str], warnings: list[str]) -> list[ParsedItem]:
    """Split the item region into blocks headed by a '<description> $<amount>' line."""
    blocks: list[tuple[str, int, list[str]]] = []
    for line in body:
        if _is_summary_line(line):
            continue
        match = RE_LINE_ITEM.match(line)
        # A "Reg Price$11.00" / "Discount: ..." line also ends in money but
        # belongs to the block above it, never starts a new one.
        is_modifier = bool(RE_REG_PRICE.search(line) or RE_ANY_DISCOUNT.search(line))
        if match and not is_modifier:
            desc = match.group("desc").strip(" .-–—")
            if desc:
                blocks.append((desc, to_cents(match.group("amount")), []))
                continue
        if blocks:
            blocks[-1][2].append(line)

    items: list[ParsedItem] = []
    for position, (desc, paid_cents, detail_lines) in enumerate(blocks):
        qty_match = RE_QTY_PREFIX.match(desc)
        if qty_match:
            desc = qty_match.group("rest").strip()

        reg_cents = None
        discount_cents = 0
        discount_percent = None
        qualifying = False

        for line in detail_lines:
            reg_match = RE_REG_PRICE.search(line)
            if reg_match and reg_cents is None:
                reg_cents = to_cents(reg_match.group(1))
                continue

            founders = RE_FOUNDERS.search(line)
            if founders:
                qualifying = True
                discount_percent = float(founders.group(1))
                discount_cents += to_cents(founders.group(2))
                continue

            other = RE_ANY_DISCOUNT.search(line)
            if other:
                warnings.append(
                    f"{desc!r} has a non-founders discount "
                    f"({other.group(1).strip()}); it does not count toward breakeven."
                )

        if reg_cents is None:
            reg_cents = paid_cents + discount_cents

        detail = ", ".join(
            line
            for line in detail_lines
            if not RE_REG_PRICE.search(line) and not RE_ANY_DISCOUNT.search(line)
        ) or None

        items.append(
            ParsedItem(
                description=desc,
                reg_price_cents=reg_cents,
                discount_cents=discount_cents if qualifying else 0,
                paid_cents=paid_cents,
                qualifying=qualifying,
                category=classify(desc, detail) if qualifying else "other",
                serving=_find_serving(detail_lines),
                detail=detail,
                discount_percent=discount_percent,
            )
        )
    return items


def _check_consistency(receipt: ParsedReceipt) -> None:
    """Warn when the parsed numbers disagree with the printed ones."""
    for item in receipt.qualifying_items:
        expected = item.reg_price_cents - item.discount_cents
        if expected != item.paid_cents:
            receipt.warnings.append(
                f"{item.description!r}: regular price minus discount is "
                f"${expected / 100:,.2f} but the line charged "
                f"${item.paid_cents / 100:,.2f}. Check this one."
            )

    if receipt.subtotal_cents is not None and receipt.items:
        line_sum = sum(i.paid_cents for i in receipt.items)
        if line_sum != receipt.subtotal_cents:
            receipt.warnings.append(
                f"Line items add to ${line_sum / 100:,.2f} but the receipt "
                f"subtotal is ${receipt.subtotal_cents / 100:,.2f}. Some lines "
                "may not have been read correctly."
            )


def parse_pdf(pdf_path: str | Path) -> ParsedReceipt:
    return parse_text(extract_text(pdf_path))

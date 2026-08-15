"""Parser tests.

The fixture mirrors the real Square receipt layout (Gmail print-to-PDF), with
the Gmail chrome, a page break in the middle of an item, food that carries no
member discount, and two founders-discounted pours.
"""
import pytest

from app.receipt_parser import ReceiptParseError, classify, parse_text, to_cents

RECEIPT = """\
8/15/26, 1:44 PM Gmail - Receipt from Obscure Wine Co. Aurburndale #XzEm
Austin <someone@example.com>
Receipt from Obscure Wine Co. Aurburndale #XzEm
1 message
Obscure Wine Co. Aurburndale <messenger@messaging.squareup.com> Sun, Aug 9, 2026 at 2:15 PM
To: someone@example.com
Obscure Wine Co. Aurburndale
Let Obscure Wine Co. Aurburndale know how your
experience was
$68.69 #XzEm
Aug 9 2026 at 2:14 PM
3 Cheeses & 2 Meats $38.00
bresaola
Served with accoutrements
https://mail.google.com/mail/u/2/?ik=071bf237fc&view=pt 1/3
8/15/26, 1:44 PM Gmail - Receipt from Obscure Wine Co. Aurburndale #XzEm
Iberico Salchichon
2022 Nebel Riesling $5.50
Grmany, Rheinhessen
Glass
Reg Price$11.00
Discount: founders (50%) (-$5.50)
2022 Bava Ruche Monferrato $10.00
Italy, Piedmont
Glass
Reg Price$20.00
Discount: founders (50%) (-$10.00)
Purchase Subtotal $53.50
Sales Tax (7%) $3.74
Tip $11.45
Total $68.69
Obscure Wine Co. Aurburndale
117 E Lake Ave Ste 102
Auburndale, FL 33823-3440
AMEX 1012 (Contactless)
Aug 9 2026 at 2:14 PM
Auth code: 839912
https://mail.google.com/mail/u/2/?ik=071bf237fc&view=pt 2/3
"""


@pytest.fixture()
def parsed():
    return parse_text(RECEIPT)


def test_reads_receipt_header(parsed):
    assert parsed.receipt_no == "XzEm"
    assert parsed.purchased_on.isoformat() == "2026-08-09"
    assert parsed.purchased_at == "2026-08-09T14:14"
    assert parsed.merchant == "Obscure Wine Co. Aurburndale"


def test_reads_totals(parsed):
    assert parsed.subtotal_cents == 5350
    assert parsed.tax_cents == 374
    assert parsed.tip_cents == 1145
    assert parsed.total_cents == 6869


def test_finds_every_line_item_including_across_the_page_break(parsed):
    assert [item.description for item in parsed.items] == [
        "3 Cheeses & 2 Meats",
        "2022 Nebel Riesling",
        "2022 Bava Ruche Monferrato",
    ]


def test_only_founders_discounted_lines_qualify(parsed):
    qualifying = [item.description for item in parsed.qualifying_items]
    assert qualifying == ["2022 Nebel Riesling", "2022 Bava Ruche Monferrato"]
    # The cheese board is kept for context but contributes nothing.
    food = parsed.items[0]
    assert food.qualifying is False
    assert food.discount_cents == 0


def test_savings_and_pre_discount_exclude_food(parsed):
    assert parsed.savings_cents == 1550
    assert parsed.pre_discount_cents == 3100        # $11 + $20, not the $38 board
    assert parsed.qualifying_paid_cents == 1550


def test_reads_regular_price_and_serving(parsed):
    riesling = parsed.items[1]
    assert riesling.reg_price_cents == 1100
    assert riesling.paid_cents == 550
    assert riesling.discount_cents == 550
    assert riesling.discount_percent == 50.0
    assert riesling.serving == "Glass"
    assert riesling.category == "wine"


def test_clean_receipt_has_no_warnings(parsed):
    assert parsed.warnings == []


def test_gmail_chrome_is_not_mistaken_for_an_item(parsed):
    for item in parsed.items:
        assert "Gmail" not in item.description
        assert not item.description.startswith("https")


def test_non_founders_discount_does_not_count():
    text = (
        "$10.00 #AB12\n"
        "Aug 9 2026 at 2:14 PM\n"
        "House Red $8.00\n"
        "Reg Price$10.00\n"
        "Discount: happy hour (20%) (-$2.00)\n"
        "Total $10.00\n"
    )
    receipt = parse_text(text)
    assert receipt.savings_cents == 0
    assert receipt.qualifying_items == []
    assert any("non-founders discount" in w for w in receipt.warnings)


def test_receipt_without_any_founders_line_is_flagged():
    text = "$38.00 #CD34\nAug 9 2026 at 2:14 PM\nCheese Board $38.00\nTotal $38.00\n"
    receipt = parse_text(text)
    assert receipt.savings_cents == 0
    assert any("counts toward breakeven" in w for w in receipt.warnings)


def test_mismatched_line_math_is_flagged():
    text = (
        "$9.00 #EF56\n"
        "Aug 9 2026 at 2:14 PM\n"
        "Odd Pour $9.00\n"
        "Reg Price$20.00\n"
        "Discount: founders (50%) (-$10.00)\n"
        "Total $9.00\n"
    )
    receipt = parse_text(text)
    assert receipt.savings_cents == 1000
    assert any("Check this one" in w for w in receipt.warnings)


def test_subtotal_mismatch_is_flagged():
    text = (
        "$5.50 #GH78\n"
        "Aug 9 2026 at 2:14 PM\n"
        "Riesling $5.50\n"
        "Reg Price$11.00\n"
        "Discount: founders (50%) (-$5.50)\n"
        "Purchase Subtotal $99.00\n"
        "Total $99.00\n"
    )
    receipt = parse_text(text)
    assert any("subtotal" in w for w in receipt.warnings)


def test_quantity_prefix_is_stripped():
    text = (
        "$11.00 #IJ90\n"
        "Aug 9 2026 at 2:14 PM\n"
        "2 x 2022 Nebel Riesling $11.00\n"
        "Reg Price$22.00\n"
        "Discount: founders (50%) (-$11.00)\n"
        "Total $11.00\n"
    )
    receipt = parse_text(text)
    assert receipt.items[0].description == "2022 Nebel Riesling"


def test_thousands_separator_parses():
    text = (
        "$600.00 #KL12\n"
        "Aug 9 2026 at 2:14 PM\n"
        "Magnum Vertical $600.00\n"
        "Reg Price$1,200.00\n"
        "Discount: founders (50%) (-$600.00)\n"
        "Total $600.00\n"
    )
    receipt = parse_text(text)
    assert receipt.savings_cents == 60000
    assert receipt.pre_discount_cents == 120000


def test_empty_text_raises():
    with pytest.raises(ReceiptParseError):
        parse_text("   \n  \n")


def test_to_cents_rounds_correctly():
    assert to_cents("31.00") == 3100
    assert to_cents("31") == 3100
    assert to_cents("1,234.56") == 123456
    assert to_cents("$5.50") == 550


def test_classify_splits_wine_and_beer():
    assert classify("2022 Nebel Riesling") == "wine"
    assert classify("Schellen Bell Alpine IPA") == "beer"
    assert classify("Weihenstephaner Hefeweizen") == "beer"
    # A discounted pour we cannot place still lands on wine, never 'other',
    # because the founders discount only applies to wine and beer.
    assert classify("Mystery Pour") == "wine"

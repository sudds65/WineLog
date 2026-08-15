#!/usr/bin/env python3
"""WineLog admin CLI.

    python manage.py init                  # create the database
    python manage.py create-user austin    # add a login (prompts for password)
    python manage.py set-password austin
    python manage.py list-users
    python manage.py seed                  # load the purchases from the spreadsheet
    python manage.py import path/to.pdf    # ingest a receipt from the shell
    python manage.py stats
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import auth, config, db, service  # noqa: E402
from app.receipt_parser import ReceiptParseError, parse_pdf  # noqa: E402

SEED_FILE = Path(__file__).resolve().parent / "app" / "seed_data.json"


def _money(cents: int | None) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def _prompt_password(confirm: bool = True) -> str:
    password = getpass.getpass("Password: ")
    if confirm and password != getpass.getpass("Confirm password: "):
        sys.exit("Passwords do not match.")
    if len(password) < 10:
        sys.exit("Password must be at least 10 characters.")
    return password


def cmd_init(_: argparse.Namespace) -> None:
    db.init_db()
    print(f"Database ready at {config.DB_PATH}")


def cmd_create_user(args: argparse.Namespace) -> None:
    db.init_db()
    password = args.password or _prompt_password()
    with db.get_conn() as conn:
        try:
            with db.transaction(conn):
                auth.create_user(conn, args.username, password)
        except sqlite3.IntegrityError:
            sys.exit(f"User {args.username!r} already exists.")
        except ValueError as exc:
            sys.exit(str(exc))
    print(f"Created user {args.username!r}.")


def cmd_set_password(args: argparse.Namespace) -> None:
    password = args.password or _prompt_password()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (args.username,)
        ).fetchone()
        if row is None:
            sys.exit(f"No such user: {args.username}")
        with db.transaction(conn):
            auth.set_password(conn, row["id"], password)
    print(f"Password updated for {args.username!r}. Other sessions were signed out.")


def cmd_list_users(_: argparse.Namespace) -> None:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT username, created_at FROM users ORDER BY id").fetchall()
    if not rows:
        print("No users yet. Run: python manage.py create-user <name>")
    for row in rows:
        print(f"{row['username']:20} created {row['created_at'][:10]}")


def cmd_seed(args: argparse.Namespace) -> None:
    """Load the receipt-level seed file (real receipts, real line items)."""
    db.init_db()
    if not SEED_FILE.exists():
        sys.exit(f"No seed file at {SEED_FILE}")
    document = json.loads(SEED_FILE.read_text())
    receipts = document.get("receipts", [])

    with db.get_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM receipts").fetchone()["n"]
        if existing and not args.force:
            sys.exit(
                f"{existing} receipt(s) already logged. Re-run with --force to load "
                "the seed file anyway (duplicates are skipped)."
            )

        for entry in receipts:
            items = entry.get("items", [])
            try:
                service.save_receipt(
                    conn,
                    purchased_on=entry["purchased_on"],
                    items=items,
                    source=entry.get("source", "pdf"),
                    receipt_no=entry.get("receipt_no"),
                    purchased_at=entry.get("purchased_at"),
                    merchant=entry.get("merchant"),
                    subtotal_cents=entry.get("subtotal_cents"),
                    tax_cents=entry.get("tax_cents"),
                    tip_cents=entry.get("tip_cents"),
                    total_cents=entry.get("total_cents"),
                    note=entry.get("note"),
                )
            except sqlite3.IntegrityError:
                print(f"  skipped duplicate receipt: {entry.get('receipt_no') or entry['purchased_on']}")
                continue

            saved = sum(i["discount_cents"] for i in items if i.get("qualifying"))
            print(f"  + {entry['purchased_on']}  #{entry.get('receipt_no') or '—'}  saved {_money(saved)}")
            for item in items:
                mark = "✓" if item.get("qualifying") else "·"
                print(f"      {mark} {item['description'][:44]:44} {_money(item['discount_cents']):>9}")

        summary = service.stats(conn)

    print(
        f"\nSaved so far: {_money(summary['saved_cents'])} of "
        f"{_money(summary['target_cents'])} — {_money(summary['remaining_cents'])} to breakeven."
    )
    print("Add the rest of your receipts with: python manage.py import <file.pdf>")


def cmd_import(args: argparse.Namespace) -> None:
    db.init_db()
    paths: list[Path] = []
    for raw in args.pdf:
        path = Path(raw)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.pdf")))
        elif path.exists():
            paths.append(path)
        else:
            print(f"! no such file: {path}")
    if not paths:
        sys.exit("Nothing to import.")

    imported = 0
    for path in paths:
        print(f"\n{path.name}")
        try:
            parsed = parse_pdf(path)
        except ReceiptParseError as exc:
            print(f"  ! {exc}")
            continue

        print(f"  Receipt {parsed.receipt_no or '(no number)'} — {parsed.purchased_on}")
        for item in parsed.items:
            mark = "✓" if item.qualifying else "·"
            print(
                f"  {mark} {item.description[:38]:38} list {_money(item.reg_price_cents):>10} "
                f"saved {_money(item.discount_cents):>9}"
            )
        print(f"  Counts toward breakeven: {_money(parsed.savings_cents)}")
        for warning in parsed.warnings:
            print(f"  ! {warning}")

        if not parsed.purchased_on:
            print("  ! no purchase date found; add this one through the web app")
            continue
        if args.dry_run:
            continue

        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        with db.get_conn() as conn:
            if service.find_duplicate(conn, parsed.receipt_no, sha):
                print("  · already logged, skipping")
                continue
            service.save_receipt(
                conn,
                purchased_on=parsed.purchased_on.isoformat(),
                items=[
                    {
                        "description": i.description,
                        "detail": i.detail,
                        "category": i.category,
                        "serving": i.serving,
                        "reg_price_cents": i.reg_price_cents,
                        "discount_cents": i.discount_cents,
                        "paid_cents": i.paid_cents,
                        "qualifying": i.qualifying,
                    }
                    for i in parsed.items
                ],
                source="pdf",
                receipt_no=parsed.receipt_no,
                purchased_at=parsed.purchased_at,
                merchant=parsed.merchant,
                subtotal_cents=parsed.subtotal_cents,
                tax_cents=parsed.tax_cents,
                tip_cents=parsed.tip_cents,
                total_cents=parsed.total_cents,
                filename=path.name,
                file_sha256=sha,
            )
            imported += 1
            print("  → logged")

    if imported:
        with db.get_conn() as conn:
            summary = service.stats(conn)
        print(
            f"\nImported {imported} receipt(s). {_money(summary['remaining_cents'])} to breakeven."
        )


def cmd_stats(_: argparse.Namespace) -> None:
    with db.get_conn() as conn:
        summary = service.stats(conn)
    print(f"Target        {_money(summary['target_cents'])}")
    print(f"Saved         {_money(summary['saved_cents'])}  ({summary['progress_percent']}%)")
    print(f"Remaining     {_money(summary['remaining_cents'])}")
    print(f"Visits        {summary['visit_count']}")
    print(f"Projected     {summary['projected_breakeven'] or 'not enough data'}")
    print(f"Membership to {summary['term_end']}  (days left: {summary['days_left']})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)

    p = sub.add_parser("create-user", help="add a login")
    p.add_argument("username")
    p.add_argument("--password", help="skip the prompt (avoid on a shared shell)")
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("set-password", help="change a password")
    p.add_argument("username")
    p.add_argument("--password")
    p.set_defaults(func=cmd_set_password)

    sub.add_parser("list-users").set_defaults(func=cmd_list_users)

    p = sub.add_parser("seed", help="load app/seed_data.json")
    p.add_argument("--force", action="store_true", help="seed even if receipts exist")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("import", help="ingest receipt PDFs (files or a directory)")
    p.add_argument("pdf", nargs="+")
    p.add_argument("--dry-run", action="store_true", help="parse and print, do not save")
    p.set_defaults(func=cmd_import)

    sub.add_parser("stats").set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

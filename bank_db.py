"""
bank_db.py - SQLite layer for the Banking bot (bank.db): accounts, savings, loans,
bonds and an append-only ledger. The coin wallet lives in Restocker (HTTP API).
"""

from __future__ import annotations

import os
import sqlite3
import contextlib
from datetime import datetime, timezone

DB_PATH = os.getenv("BANK_DB_PATH", os.path.join(os.path.dirname(__file__), "bank.db"))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                user_id       TEXT PRIMARY KEY,
                name          TEXT,
                created_at    TEXT NOT NULL,
                opted_in      INTEGER NOT NULL DEFAULT 1,
                frozen        INTEGER NOT NULL DEFAULT 0,
                frozen_reason TEXT,
                frozen_at     TEXT,
                credit_limit  INTEGER
            );

            CREATE TABLE IF NOT EXISTS savings (
                user_id       TEXT PRIMARY KEY,
                balance       REAL NOT NULL DEFAULT 0,
                last_accrued  TEXT,
                FOREIGN KEY (user_id) REFERENCES accounts(user_id)
            );

            CREATE TABLE IF NOT EXISTS loans (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL,
                principal     REAL NOT NULL,
                balance       REAL NOT NULL,
                apr           REAL NOT NULL,
                -- pending | active | paid | denied | written_off
                status        TEXT NOT NULL DEFAULT 'active',
                issued_at     TEXT NOT NULL,
                due_at        TEXT,
                last_accrued  TEXT,
                term_days     INTEGER,
                requested_at  TEXT,
                decided_by    TEXT,
                decided_at    TEXT,
                overdue_notified INTEGER NOT NULL DEFAULT 0,
                collected     REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES accounts(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_loans_user ON loans(user_id, status);

            CREATE TABLE IF NOT EXISTS bonds (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL,
                principal     REAL NOT NULL,
                apr           REAL NOT NULL,
                term_days     INTEGER NOT NULL,
                payout        REAL NOT NULL,   -- principal + fixed interest, paid at maturity
                status        TEXT NOT NULL DEFAULT 'active',  -- active | redeemed
                issued_at     TEXT NOT NULL,
                matures_at    TEXT NOT NULL,
                redeemed_at   TEXT,
                redeemed_amount REAL,
                FOREIGN KEY (user_id) REFERENCES accounts(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_bonds_user ON bonds(user_id, status);

            CREATE TABLE IF NOT EXISTS ledger (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   TEXT NOT NULL,
                kind      TEXT NOT NULL,   -- deposit|withdraw|transfer_out|transfer_in|loan_out|loan_repay|interest_savings|interest_loan|stock_buy|stock_sell|bond_buy|bond_redeem|account_closed
                amount    REAL NOT NULL,
                meta      TEXT,
                ts        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, ts);
            """
        )
        _migrate(conn)


# Columns added after the first release. CREATE TABLE IF NOT EXISTS is a no-op on
# a database that already exists, so every column added above must also be listed
# here or an already-deployed bank.db would never gain it.
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "accounts": [
        ("frozen", "INTEGER NOT NULL DEFAULT 0"),
        ("frozen_reason", "TEXT"),
        ("frozen_at", "TEXT"),
        ("credit_limit", "INTEGER"),
    ],
    "loans": [
        ("term_days", "INTEGER"),
        ("requested_at", "TEXT"),
        ("decided_by", "TEXT"),
        ("decided_at", "TEXT"),
        ("overdue_notified", "INTEGER NOT NULL DEFAULT 0"),
        ("collected", "REAL NOT NULL DEFAULT 0"),
    ],
}


def _migrate(conn) -> None:
    """Add any columns missing from an older bank.db. Idempotent."""
    for table, cols in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")



def get_account(user_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE user_id=?", (str(user_id),)).fetchone()
        return dict(row) if row else None


def open_account(user_id: str, name: str | None) -> dict:
    with db() as conn:
        conn.execute(
            "INSERT INTO accounts (user_id, name, created_at, opted_in) VALUES (?,?,?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, opted_in=1",
            (str(user_id), name or "", utcnow()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO savings (user_id, balance, last_accrued) VALUES (?,0,?)",
            (str(user_id), utcnow()),
        )
        row = conn.execute("SELECT * FROM accounts WHERE user_id=?", (str(user_id),)).fetchone()
        return dict(row)


def close_account(user_id: str) -> None:
    """Soft-close an account by flipping opted_in to 0. The accounts row,
    savings row, and all loan/bond/ledger history are kept (deleting the row
    outright would violate the FK constraints from loans/bonds, and would
    destroy the audit trail). /bank open's ON CONFLICT ... opted_in=1 clause
    reopens it later — same row, same history."""
    with db() as conn:
        conn.execute("UPDATE accounts SET opted_in=0 WHERE user_id=?", (str(user_id),))



def set_frozen(user_id: str, frozen: bool, reason: str = "") -> None:
    """Freeze/unfreeze an account. A frozen account can still be read
    (/bank balance, /loan status) but cannot move money."""
    with db() as conn:
        conn.execute(
            "UPDATE accounts SET frozen=?, frozen_reason=?, frozen_at=? WHERE user_id=?",
            (1 if frozen else 0, reason if frozen else None,
             utcnow() if frozen else None, str(user_id)),
        )


def set_credit_limit(user_id: str, limit: int | None) -> None:
    """Override a user's borrowing cap. None clears the override so the
    history-based limit applies again."""
    with db() as conn:
        conn.execute("UPDATE accounts SET credit_limit=? WHERE user_id=?",
                     (int(limit) if limit is not None else None, str(user_id)))


def all_accounts(active_only: bool = True) -> list[dict]:
    with db() as conn:
        sql = "SELECT * FROM accounts"
        if active_only:
            sql += " WHERE opted_in=1"
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at").fetchall()]


def get_savings(user_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM savings WHERE user_id=?", (str(user_id),)).fetchone()
        if row:
            return dict(row)
        return {"user_id": str(user_id), "balance": 0.0, "last_accrued": None}


def add_savings(user_id: str, delta: float) -> float:
    """Adjust the savings vault by delta (can be negative). Returns new balance.
    Use this for CREDITS (deposits, refunds, interest). For debits that must not
    overdraw, use try_debit_savings() so the check-and-debit is atomic."""
    with db() as conn:
        conn.execute(
            "INSERT INTO savings (user_id, balance, last_accrued) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance = savings.balance + ?, last_accrued = excluded.last_accrued",
            (str(user_id), max(0.0, delta), utcnow(), delta),
        )
        row = conn.execute("SELECT balance FROM savings WHERE user_id=?", (str(user_id),)).fetchone()
        return float(row["balance"]) if row else 0.0


def try_debit_savings(user_id: str, amount: float) -> bool:
    """Atomically subtract `amount` from savings ONLY if the balance is enough.
    Returns True if it happened, False if there weren't enough funds.

    This single conditional UPDATE closes the race where two simultaneous
    withdrawals both read the same balance and both pass an 'enough?' check
    before either deducts (which would let a user withdraw more than they have).
    """
    if amount <= 0:
        return False
    with db() as conn:
        cur = conn.execute(
            "UPDATE savings SET balance = balance - ? WHERE user_id=? AND balance >= ?",
            (amount, str(user_id), amount),
        )
        return cur.rowcount == 1


def set_savings_accrued(user_id: str, when: str, new_balance: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE savings SET balance=?, last_accrued=? WHERE user_id=?",
            (new_balance, when, str(user_id)),
        )


def all_savings() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM savings WHERE balance > 0").fetchall()]



def create_loan(user_id: str, principal: float, apr: float, due_at: str | None,
                *, status: str = "active", term_days: int | None = None) -> dict:
    """Create a loan row. status='pending' parks it awaiting staff approval —
    interest doesn't accrue and it isn't counted as debt until approved."""
    now = utcnow()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO loans (user_id, principal, balance, apr, status, issued_at, due_at, "
            "last_accrued, term_days, requested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(user_id), principal, principal, apr, status, now, due_at,
             now if status == "active" else None, term_days, now),
        )
        row = conn.execute("SELECT * FROM loans WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_loan(loan_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM loans WHERE id=?", (int(loan_id),)).fetchone()
        return dict(row) if row else None


def get_pending_loans(user_id: str | None = None) -> list[dict]:
    with db() as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT * FROM loans WHERE status='pending' ORDER BY requested_at").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM loans WHERE status='pending' AND user_id=? ORDER BY requested_at",
                (str(user_id),)).fetchall()
        return [dict(r) for r in rows]


def claim_pending_loan(loan_id: int, decider_id: str) -> bool:
    """Atomically take a loan out of 'pending' into 'approving' so two staff
    clicking Approve at the same moment can't both disburse it. Returns True if
    THIS call won. Follow with finalize_loan_approval() or release_pending_loan()."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE loans SET status='approving', decided_by=?, decided_at=? "
            "WHERE id=? AND status='pending'",
            (str(decider_id), utcnow(), int(loan_id)),
        )
        return cur.rowcount == 1


def release_pending_loan(loan_id: int) -> None:
    """Put a claimed loan back to 'pending' — used when disbursement failed."""
    with db() as conn:
        conn.execute(
            "UPDATE loans SET status='pending', decided_by=NULL, decided_at=NULL "
            "WHERE id=? AND status='approving'", (int(loan_id),))


def finalize_loan_approval(loan_id: int, due_at: str | None) -> dict:
    """Activate a claimed loan once the coins actually landed in the wallet.
    issued_at/last_accrued are reset to now so interest runs from disbursement,
    not from when the request was filed."""
    now = utcnow()
    with db() as conn:
        conn.execute(
            "UPDATE loans SET status='active', issued_at=?, last_accrued=?, due_at=? WHERE id=?",
            (now, now, due_at, int(loan_id)),
        )
        return dict(conn.execute("SELECT * FROM loans WHERE id=?", (int(loan_id),)).fetchone())


def deny_loan(loan_id: int, decider_id: str) -> bool:
    """Atomically flip a pending loan to 'denied'. Returns True if THIS call won."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE loans SET status='denied', decided_by=?, decided_at=? "
            "WHERE id=? AND status='pending'",
            (str(decider_id), utcnow(), int(loan_id)),
        )
        return cur.rowcount == 1


def write_off_loan(loan_id: int, decider_id: str) -> dict | None:
    """Forgive a loan: zero the balance and mark it written_off. No coins move —
    the debt simply stops existing."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE loans SET balance=0, status='written_off', decided_by=?, decided_at=? "
            "WHERE id=? AND status='active'",
            (str(decider_id), utcnow(), int(loan_id)),
        )
        if cur.rowcount != 1:
            return None
        return dict(conn.execute("SELECT * FROM loans WHERE id=?", (int(loan_id),)).fetchone())


def overdue_loans(now_iso: str | None = None) -> list[dict]:
    """Every active loan past its due date, worst first."""
    now_iso = now_iso or utcnow()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM loans WHERE status='active' AND due_at IS NOT NULL "
            "AND due_at < ? AND balance > 0 ORDER BY due_at",
            (now_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_overdue_notified(loan_id: int) -> bool:
    """Flag a loan as 'we've already announced this one'. Returns True the first
    time only, so the reminder fires once per loan rather than every pass."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE loans SET overdue_notified=1 WHERE id=? AND overdue_notified=0",
            (int(loan_id),))
        return cur.rowcount == 1


def record_collection(loan_id: int, amount: float) -> None:
    with db() as conn:
        conn.execute("UPDATE loans SET collected = collected + ? WHERE id=?",
                     (float(amount), int(loan_id)))


def loan_history(user_id: str) -> dict:
    """Repayment track record, used to size the credit limit."""
    with db() as conn:
        rows = conn.execute(
            "SELECT status, principal, due_at, decided_at, overdue_notified "
            "FROM loans WHERE user_id=?", (str(user_id),)).fetchall()
    repaid = [r for r in rows if r["status"] == "paid"]
    return {
        "repaid_count": len(repaid),
        "repaid_total": sum(float(r["principal"]) for r in repaid),
        "written_off_count": sum(1 for r in rows if r["status"] == "written_off"),
        "denied_count": sum(1 for r in rows if r["status"] == "denied"),
        # a loan that ever went overdue is a black mark even once repaid
        "late_count": sum(1 for r in rows if r["overdue_notified"]),
    }


def get_active_loans(user_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM loans WHERE user_id=? AND status='active' ORDER BY issued_at",
            (str(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def all_active_loans() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM loans WHERE status='active'").fetchall()]


def apply_loan_payment(loan_id: int, amount: float) -> dict:
    """Reduce a loan's balance by amount; mark paid if it reaches zero."""
    with db() as conn:
        row = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not row:
            raise ValueError("loan not found")
        new_bal = max(0.0, float(row["balance"]) - amount)
        status = "paid" if new_bal <= 1e-9 else "active"
        conn.execute(
            "UPDATE loans SET balance=?, status=? WHERE id=?",
            (new_bal, status, loan_id),
        )
        row = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        return dict(row)


def set_loan_accrued(loan_id: int, when: str, new_balance: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE loans SET balance=?, last_accrued=? WHERE id=?",
            (new_balance, when, loan_id),
        )


def total_debt(user_id: str) -> float:
    return sum(float(l["balance"]) for l in get_active_loans(user_id))



def create_bond(user_id: str, principal: float, apr: float, term_days: int,
                payout: float, matures_at: str) -> dict:
    now = utcnow()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO bonds (user_id, principal, apr, term_days, payout, status, issued_at, matures_at) "
            "VALUES (?,?,?,?,?, 'active', ?, ?)",
            (str(user_id), principal, apr, int(term_days), payout, now, matures_at),
        )
        row = conn.execute("SELECT * FROM bonds WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_bond(bond_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM bonds WHERE id=?", (int(bond_id),)).fetchone()
        return dict(row) if row else None


def get_bonds(user_id: str, status: str | None = "active") -> list[dict]:
    with db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM bonds WHERE user_id=? AND status=? ORDER BY matures_at",
                (str(user_id), status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bonds WHERE user_id=? ORDER BY issued_at DESC",
                (str(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]


def claim_bond(bond_id: int) -> bool:
    """Atomically flip a bond from 'active' to 'redeemed'. Returns True if THIS
    call won the flip, False if it was already redeemed. Call this BEFORE paying
    out so two simultaneous /bond redeem commands can't pay the same bond twice.
    On payout failure, call unclaim_bond() to revert."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE bonds SET status='redeemed' WHERE id=? AND status='active'",
            (int(bond_id),),
        )
        return cur.rowcount == 1


def unclaim_bond(bond_id: int) -> None:
    """Revert a claim if the payout failed, so the bond stays redeemable."""
    with db() as conn:
        conn.execute("UPDATE bonds SET status='active' WHERE id=?", (int(bond_id),))


def finalize_bond_redemption(bond_id: int, amount: float, when: str | None = None) -> dict:
    """Record what was paid out after a successful claim+payout."""
    when = when or utcnow()
    with db() as conn:
        conn.execute(
            "UPDATE bonds SET redeemed_at=?, redeemed_amount=? WHERE id=?",
            (when, float(amount), int(bond_id)),
        )
        row = conn.execute("SELECT * FROM bonds WHERE id=?", (int(bond_id),)).fetchone()
        return dict(row)


def total_bonds_value(user_id: str) -> float:
    """Sum of payout-at-maturity for a user's active bonds (locked value)."""
    return sum(float(b["payout"]) for b in get_bonds(user_id, "active"))



def bank_stats() -> dict:
    """Bank-wide totals for /admin stats."""
    with db() as conn:
        def one(sql, *a):
            return conn.execute(sql, a).fetchone()[0] or 0
        return {
            "accounts_open": one("SELECT COUNT(*) FROM accounts WHERE opted_in=1"),
            "accounts_closed": one("SELECT COUNT(*) FROM accounts WHERE opted_in=0"),
            "accounts_frozen": one("SELECT COUNT(*) FROM accounts WHERE frozen=1"),
            "savings_total": one("SELECT SUM(balance) FROM savings"),
            "loans_active": one("SELECT COUNT(*) FROM loans WHERE status='active'"),
            "loans_pending": one("SELECT COUNT(*) FROM loans WHERE status='pending'"),
            "debt_total": one("SELECT SUM(balance) FROM loans WHERE status='active'"),
            "written_off_total": one("SELECT SUM(principal) FROM loans WHERE status='written_off'"),
            "bonds_active": one("SELECT COUNT(*) FROM bonds WHERE status='active'"),
            "bonds_locked": one("SELECT SUM(principal) FROM bonds WHERE status='active'"),
            "bonds_payout": one("SELECT SUM(payout) FROM bonds WHERE status='active'"),
        }


def log(user_id: str, kind: str, amount: float, meta: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO ledger (user_id, kind, amount, meta, ts) VALUES (?,?,?,?,?)",
            (str(user_id), kind, float(amount), meta, utcnow()),
        )


def recent_ledger(user_id: str, limit: int = 10) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (str(user_id), limit),
        ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"Initialized bank DB at {DB_PATH}")

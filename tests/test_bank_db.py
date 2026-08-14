"""
Dummy-data regression tests for the Bank bot SQLite layer.

Run from the Bank bot folder:
    python -m pytest tests/ -q
or standalone (no pytest needed):
    python tests/test_bank_db.py

Every test uses a throwaway temp DB — it never touches the live bank.db.
"""
import importlib.util
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DBFILE = _HERE.parent / "bank_db.py"


def _fresh_bank():
    os.environ["BANK_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "bank.db")
    spec = importlib.util.spec_from_file_location("bank_db_test", str(_DBFILE))
    bdb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bdb)
    bdb.init_db()
    return bdb


def test_accounts():
    b = _fresh_bank()
    b.open_account("1", "alice")
    assert b.get_account("1") is not None
    # idempotent
    b.open_account("1", "alice")
    assert b.get_account("1") is not None


def test_savings_overdraft_guard():
    b = _fresh_bank()
    b.open_account("1", "alice")
    b.add_savings("1", 100.0)
    assert b.get_savings("1")["balance"] == 100.0
    assert b.try_debit_savings("1", 40.0) is True
    assert b.get_savings("1")["balance"] == 60.0
    # cannot overdraw
    assert b.try_debit_savings("1", 999.0) is False
    assert b.get_savings("1")["balance"] == 60.0


def test_loans():
    b = _fresh_bank()
    b.open_account("1", "alice")
    loan = b.create_loan("1", 200.0, 0.18, None)
    assert abs(b.total_debt("1") - 200.0) < 0.01
    b.apply_loan_payment(loan["id"], 50.0)
    assert abs(b.total_debt("1") - 150.0) < 0.01
    # overpay must not go negative and should close the loan
    b.apply_loan_payment(loan["id"], 100000.0)
    assert b.total_debt("1") >= 0
    assert len(b.get_active_loans("1")) == 0


def test_bonds_claim_lock_and_redeem():
    b = _fresh_bank()
    b.open_account("1", "alice")
    mat = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    bond = b.create_bond("1", 500.0, 0.10, 30, payout=504.0, matures_at=mat)
    bid = bond["id"]
    assert any(x["id"] == bid for x in b.get_bonds("1", "active"))
    # claim lock prevents a double-redeem race
    assert b.claim_bond(bid) is True
    assert b.claim_bond(bid) is False
    b.finalize_bond_redemption(bid, 504.0)
    assert not any(x["id"] == bid for x in b.get_bonds("1", "active"))


def test_freeze_and_credit_limit_override():
    b = _fresh_bank()
    b.open_account("1", "alice")
    assert not b.get_account("1")["frozen"]
    b.set_frozen("1", True, "suspected exploit")
    a = b.get_account("1")
    assert a["frozen"] == 1 and a["frozen_reason"] == "suspected exploit"
    b.set_frozen("1", False)
    a = b.get_account("1")
    assert a["frozen"] == 0 and a["frozen_reason"] is None

    assert b.get_account("1")["credit_limit"] is None
    b.set_credit_limit("1", 25_000)
    assert b.get_account("1")["credit_limit"] == 25_000
    b.set_credit_limit("1", None)
    assert b.get_account("1")["credit_limit"] is None


def test_pending_loan_is_not_debt_until_approved():
    b = _fresh_bank()
    b.open_account("1", "alice")
    loan = b.create_loan("1", 5000.0, 0.18, None, status="pending", term_days=30)
    # a request on the table is not money owed
    assert b.total_debt("1") == 0
    assert b.get_active_loans("1") == []
    assert [p["id"] for p in b.get_pending_loans("1")] == [loan["id"]]

    assert b.claim_pending_loan(loan["id"], "99") is True
    due = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    b.finalize_loan_approval(loan["id"], due)
    assert abs(b.total_debt("1") - 5000.0) < 0.01
    assert b.get_pending_loans("1") == []


def test_approve_race_only_one_winner():
    b = _fresh_bank()
    b.open_account("1", "alice")
    loan = b.create_loan("1", 100.0, 0.18, None, status="pending", term_days=7)
    assert b.claim_pending_loan(loan["id"], "banker_a") is True
    # second banker clicking Approve a beat later must lose
    assert b.claim_pending_loan(loan["id"], "banker_b") is False
    # and Deny must not be able to steal a loan already being disbursed
    assert b.deny_loan(loan["id"], "banker_b") is False
    # a failed disbursement puts it back on the table
    b.release_pending_loan(loan["id"])
    assert b.get_loan(loan["id"])["status"] == "pending"
    assert b.deny_loan(loan["id"], "banker_b") is True
    assert b.deny_loan(loan["id"], "banker_c") is False


def test_overdue_detection_and_single_notification():
    b = _fresh_bank()
    b.open_account("1", "alice")
    past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    late = b.create_loan("1", 300.0, 0.18, past)
    b.create_loan("1", 300.0, 0.18, future)
    ids = [l["id"] for l in b.overdue_loans()]
    assert ids == [late["id"]]
    # the reminder fires exactly once, not once per hourly pass
    assert b.mark_overdue_notified(late["id"]) is True
    assert b.mark_overdue_notified(late["id"]) is False


def test_write_off_and_history():
    b = _fresh_bank()
    b.open_account("1", "alice")
    paid = b.create_loan("1", 1000.0, 0.18, None)
    b.apply_loan_payment(paid["id"], 1000.0)
    bad = b.create_loan("1", 500.0, 0.18, None)
    assert b.write_off_loan(bad["id"], "banker") is not None
    # writing off twice is a no-op, not a second loss
    assert b.write_off_loan(bad["id"], "banker") is None
    assert b.total_debt("1") == 0
    h = b.loan_history("1")
    assert h["repaid_count"] == 1 and h["written_off_count"] == 1


def test_collection_cannot_overdraw_savings():
    b = _fresh_bank()
    b.open_account("1", "alice")
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    loan = b.create_loan("1", 1000.0, 0.18, past)
    b.add_savings("1", 250.0)
    # the bank can only take what it holds
    assert b.try_debit_savings("1", 250.0) is True
    b.apply_loan_payment(loan["id"], 250.0)
    b.record_collection(loan["id"], 250.0)
    assert b.get_savings("1")["balance"] == 0
    assert abs(b.total_debt("1") - 750.0) < 0.01
    assert abs(b.get_loan(loan["id"])["collected"] - 250.0) < 0.01
    assert b.try_debit_savings("1", 1.0) is False


def test_migration_from_pre_v2_schema():
    """An already-deployed bank.db predates the new columns. CREATE TABLE IF NOT
    EXISTS won't add them, so init_db() must ALTER them in — and keep the rows."""
    import sqlite3
    path = Path(tempfile.mkdtemp()) / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE accounts (user_id TEXT PRIMARY KEY, name TEXT,
            created_at TEXT NOT NULL, opted_in INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE savings (user_id TEXT PRIMARY KEY, balance REAL NOT NULL DEFAULT 0,
            last_accrued TEXT);
        CREATE TABLE loans (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            principal REAL NOT NULL, balance REAL NOT NULL, apr REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', issued_at TEXT NOT NULL,
            due_at TEXT, last_accrued TEXT);
        INSERT INTO accounts VALUES ('1','alice','2026-01-01T00:00:00+00:00',1);
        INSERT INTO savings VALUES ('1', 4242.0, '2026-01-01T00:00:00+00:00');
        INSERT INTO loans (user_id,principal,balance,apr,status,issued_at)
            VALUES ('1',900.0,900.0,0.18,'active','2026-01-01T00:00:00+00:00');
    """)
    con.commit()
    con.close()

    os.environ["BANK_DB_PATH"] = str(path)
    spec = importlib.util.spec_from_file_location("bank_db_migrate", str(_DBFILE))
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.init_db()

    acct = b.get_account("1")
    assert acct["name"] == "alice"          # existing data survived
    assert acct["frozen"] == 0              # new column defaulted
    assert acct["credit_limit"] is None
    assert b.get_savings("1")["balance"] == 4242.0
    loan = b.get_active_loans("1")[0]
    assert loan["overdue_notified"] == 0 and loan["collected"] == 0
    # and the new code paths work against the migrated DB
    b.set_frozen("1", True, "test")
    assert b.get_account("1")["frozen"] == 1
    assert b.bank_stats()["accounts_open"] == 1
    b.init_db()  # migration is idempotent


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")

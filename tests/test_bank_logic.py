"""
Tests for the bot-level logic in bank_main: credit limits, the collections pass,
and overdue garnishment. These import bank_main for real (no Discord connection
is made — discord.py only connects when bot.run() is called) against a throwaway
DB, and stub out the Restocker HTTP client.

    python -m pytest tests/ -q
    python tests/test_bank_logic.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

# Configure BEFORE importing bank_main — it reads env at import time.
os.environ["BANK_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "bank.db")
os.environ.update(
    BASE_CREDIT_LIMIT="10000",
    CREDIT_PER_REPAID_LOAN="5000",
    CREDIT_LATE_PENALTY="5000",
    MAX_LOAN="100000",
    COLLECT_FROM_SAVINGS="1",
    COLLECT_GRACE_DAYS="3",
    GARNISH_BOND_PAYOUTS="1",
    # keep the tests silent: no channels to post to
    NEW_ACCOUNT_CHANNEL_ID="", LOAN_PROPOSALS_CHANNEL_ID="", BOT_LOG_CHANNEL_ID="",
)

import bank_main as bm  # noqa: E402
import bank_db as bdb  # noqa: E402


def _reset(user="1"):
    """Wipe the tables so each test starts from a known state."""
    bdb.init_db()
    with bdb.db() as c:
        for t in ("ledger", "bonds", "loans", "savings", "accounts"):
            c.execute(f"DELETE FROM {t}")
    bdb.open_account(user, "tester")
    return user


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def test_credit_limit_starts_at_base():
    u = _reset()
    assert bm.credit_limit_for(u) == 10000


def test_credit_limit_grows_with_clean_repayments():
    u = _reset()
    for _ in range(3):
        loan = bdb.create_loan(u, 1000.0, 0.18, None)
        bdb.apply_loan_payment(loan["id"], 1000.0)
    assert bm.credit_limit_for(u) == 10000 + 3 * 5000


def test_credit_limit_shrinks_for_late_loans():
    u = _reset()
    loan = bdb.create_loan(u, 1000.0, 0.18, _days_ago(5))
    bdb.mark_overdue_notified(loan["id"])       # went overdue once
    bdb.apply_loan_payment(loan["id"], 1000.0)  # ...but was eventually repaid
    # +5000 for repaying, -5000 for being late = back to base
    assert bm.credit_limit_for(u) == 10000


def test_credit_limit_is_zero_after_a_write_off():
    u = _reset()
    loan = bdb.create_loan(u, 1000.0, 0.18, None)
    bdb.write_off_loan(loan["id"], "banker")
    assert bm.credit_limit_for(u) == 0


def test_credit_limit_override_beats_history():
    u = _reset()
    bdb.write_off_loan(bdb.create_loan(u, 1000.0, 0.18, None)["id"], "banker")
    assert bm.credit_limit_for(u) == 0
    bdb.set_credit_limit(u, 7500)
    assert bm.credit_limit_for(u) == 7500
    bdb.set_credit_limit(u, None)
    assert bm.credit_limit_for(u) == 0


def test_credit_limit_never_exceeds_max_loan():
    u = _reset()
    for _ in range(50):  # 50 * 5000 would be 250k
        bdb.apply_loan_payment(bdb.create_loan(u, 10.0, 0.18, None)["id"], 10.0)
    assert bm.credit_limit_for(u) == 100000


def test_collections_seize_savings_after_grace():
    u = _reset()
    loan = bdb.create_loan(u, 1000.0, 0.18, _days_ago(10))  # well past 3d grace
    bdb.add_savings(u, 400.0)
    asyncio.run(bm.run_collections())
    assert bdb.get_savings(u)["balance"] == 0
    assert abs(bdb.total_debt(u) - 600.0) < 0.01
    assert abs(bdb.get_loan(loan["id"])["collected"] - 400.0) < 0.01


def test_collections_respect_the_grace_period():
    u = _reset()
    bdb.create_loan(u, 1000.0, 0.18, _days_ago(1))  # overdue, but inside grace
    bdb.add_savings(u, 400.0)
    asyncio.run(bm.run_collections())
    assert bdb.get_savings(u)["balance"] == 400.0
    assert abs(bdb.total_debt(u) - 1000.0) < 0.01


def test_collections_ignore_loans_that_are_not_yet_due():
    u = _reset()
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    bdb.create_loan(u, 1000.0, 0.18, future)
    bdb.add_savings(u, 400.0)
    asyncio.run(bm.run_collections())
    assert bdb.get_savings(u)["balance"] == 400.0


def test_collections_never_take_more_than_is_owed():
    u = _reset()
    bdb.create_loan(u, 100.0, 0.18, _days_ago(10))
    bdb.add_savings(u, 5000.0)
    asyncio.run(bm.run_collections())
    assert bdb.total_debt(u) == 0
    assert abs(bdb.get_savings(u)["balance"] - 4900.0) < 0.01


def test_collections_are_idempotent():
    u = _reset()
    bdb.create_loan(u, 1000.0, 0.18, _days_ago(10))
    bdb.add_savings(u, 400.0)
    for _ in range(3):
        asyncio.run(bm.run_collections())
    # three passes, one seizure — the second and third find nothing left to take
    assert abs(bdb.total_debt(u) - 600.0) < 0.01
    assert bdb.get_savings(u)["balance"] == 0


def test_garnish_helper_only_touches_overdue_debt():
    u = _reset()
    overdue = bdb.create_loan(u, 500.0, 0.18, _days_ago(10))
    current = bdb.create_loan(u, 500.0, 0.18,
                              (datetime.now(timezone.utc) + timedelta(days=10)).isoformat())
    assert abs(bm._overdue_debt(u) - 500.0) < 0.01
    applied = bm._apply_to_overdue(u, 800.0, "test")
    # capped at the overdue balance; the loan that isn't late yet is untouched
    assert abs(applied - 500.0) < 0.01
    assert bdb.get_loan(overdue["id"])["status"] == "paid"
    assert abs(bdb.get_loan(current["id"])["balance"] - 500.0) < 0.01


def test_overdue_interest_uses_penalty_rate():
    """A loan past due must accrue faster than one that isn't."""
    u = _reset()
    late = bdb.create_loan(u, 10000.0, bm.LOAN_APR, _days_ago(30))
    ontime = bdb.create_loan(u, 10000.0, bm.LOAN_APR,
                             (datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
    # pretend both were last accrued 10 days ago
    bdb.set_loan_accrued(late["id"], _days_ago(10), 10000.0)
    bdb.set_loan_accrued(ontime["id"], _days_ago(10), 10000.0)
    asyncio.run(bm.accrue_interest())
    late_bal = bdb.get_loan(late["id"])["balance"]
    ontime_bal = bdb.get_loan(ontime["id"])["balance"]
    assert late_bal > ontime_bal > 10000.0


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
    sys.exit(0 if passed == len(fns) else 1)

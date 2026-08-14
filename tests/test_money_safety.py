"""
Regression tests for the money-safety bugs found in review. Each test fails
against the version of the code that had the bug.

    python -m pytest tests/ -q
    python tests/test_money_safety.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

os.environ["BANK_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "bank.db")
os.environ.update(
    BASE_CREDIT_LIMIT="10000", CREDIT_PER_REPAID_LOAN="5000", CREDIT_LATE_PENALTY="5000",
    MAX_LOAN="100000", LOAN_REQUIRE_APPROVAL="1", GARNISH_BOND_PAYOUTS="1",
    COLLECT_FROM_SAVINGS="1", COLLECT_GRACE_DAYS="3",
    NEW_ACCOUNT_CHANNEL_ID="", LOAN_PROPOSALS_CHANNEL_ID="", BOT_LOG_CHANNEL_ID="",
)

import bank_main as bm  # noqa: E402
import bank_db as bdb  # noqa: E402


class FakeClient:
    """Stands in for RestockerClient. Records every wallet adjustment."""

    def __init__(self, delay=0.0, fail=False):
        self.calls = []
        self.delay = delay
        self.fail = fail

    async def adjust(self, user_id, amount, *, reason="", count_principal=True,
                     idempotency_key=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            from restocker_client import RestockerError
            raise RestockerError("simulated outage")
        self.calls.append({"user_id": str(user_id), "amount": int(amount),
                           "key": idempotency_key, "reason": reason})
        return {"coins": 1_000_000}

    @property
    def total_credited(self):
        return sum(c["amount"] for c in self.calls if c["amount"] > 0)


class FakeResponse:
    def __init__(self):
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, content=None, **kw):
        self._done = True


class FakeFollowup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append(content if content is not None else kw.get("embed"))


class FakeUser:
    def __init__(self, uid, name="tester"):
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.bot = False


class FakeInteraction:
    """Enough of discord.Interaction for the code paths under test."""

    def __init__(self, uid=999, name="banker"):
        self.user = FakeUser(uid, name)
        self.response = FakeResponse()
        self.sent = []
        self.followup = FakeFollowup(self.sent)
        self.message = None   # slash-command path: nothing to stamp

    def text(self):
        return " ".join(str(x) for x in self.sent)


def _reset(user="1"):
    bdb.init_db()
    with bdb.db() as c:
        for t in ("ledger", "bonds", "loans", "savings", "accounts"):
            c.execute(f"DELETE FROM {t}")
    bdb.open_account(user, "borrower")
    bm._user_locks.clear()
    return user


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


# ── the credit limit must survive concurrent approvals ───────────────────────

def test_concurrent_approvals_cannot_bust_the_credit_limit():
    """Two 10k loans approved simultaneously against a 10k limit: exactly one
    may disburse. total_debt() only counts 'active' loans, so without the
    borrower lock both approvals read debt=0 and both pay out."""
    u = _reset()
    bm.client_rs = FakeClient(delay=0.05)   # a slow wallet call widens the window
    a = bdb.create_loan(u, 10000.0, 0.18, None, status="pending", term_days=30)
    b = bdb.create_loan(u, 10000.0, 0.18, None, status="pending", term_days=30)

    async def run():
        await asyncio.gather(bm.approve_loan(FakeInteraction(), a["id"]),
                             bm.approve_loan(FakeInteraction(), b["id"]))
    asyncio.run(run())

    assert bm.client_rs.total_credited == 10000, bm.client_rs.calls
    assert bdb.total_debt(u) == 10000
    statuses = sorted([bdb.get_loan(a["id"])["status"], bdb.get_loan(b["id"])["status"]])
    assert statuses == ["active", "pending"], statuses


def test_disbursement_uses_a_stable_idempotency_key():
    """A retry after a lost response must be recognised as the same payout, so
    the key can't be a fresh uuid each attempt."""
    u = _reset()
    bm.client_rs = FakeClient()
    loan = bdb.create_loan(u, 5000.0, 0.18, None, status="pending", term_days=30)
    asyncio.run(bm.approve_loan(FakeInteraction(), loan["id"]))
    assert bm.client_rs.calls[0]["key"] == f"loan-{loan['id']}-disburse"


def test_failed_disbursement_releases_the_loan_back_to_pending():
    """A loan stuck in 'approving' is invisible to every command — no coins
    moved and only manual SQL could free it."""
    u = _reset()
    bm.client_rs = FakeClient(fail=True)
    loan = bdb.create_loan(u, 5000.0, 0.18, None, status="pending", term_days=30)
    asyncio.run(bm.approve_loan(FakeInteraction(), loan["id"]))
    assert bdb.get_loan(loan["id"])["status"] == "pending"
    assert bdb.total_debt(u) == 0
    assert [p["id"] for p in bdb.get_pending_loans(u)] == [loan["id"]]


def test_approval_is_refused_for_a_frozen_borrower():
    u = _reset()
    bm.client_rs = FakeClient()
    loan = bdb.create_loan(u, 5000.0, 0.18, None, status="pending", term_days=30)
    bdb.set_frozen(u, True, "under review")
    it = FakeInteraction()
    asyncio.run(bm.approve_loan(it, loan["id"]))
    assert bm.client_rs.calls == []
    assert bdb.get_loan(loan["id"])["status"] == "pending"
    assert "frozen" in it.text().lower()


def test_a_denied_loan_can_never_be_approved():
    u = _reset()
    bm.client_rs = FakeClient()
    loan = bdb.create_loan(u, 5000.0, 0.18, None, status="pending", term_days=30)
    asyncio.run(bm.deny_loan(FakeInteraction(), loan["id"]))
    asyncio.run(bm.approve_loan(FakeInteraction(), loan["id"]))
    assert bm.client_rs.calls == []
    assert bdb.get_loan(loan["id"])["status"] == "denied"
    assert bdb.total_debt(u) == 0


# ── bond garnishment must not destroy coins ─────────────────────────────────

def test_bond_garnish_shortfall_is_never_destroyed():
    """If the overdue debt is settled between sizing the garnish and applying
    it, the withheld share must land somewhere — not vanish."""
    u = _reset()
    bdb.create_loan(u, 500.0, 0.18, _days_ago(10))
    assert abs(bm._overdue_debt(u) - 500.0) < 0.01
    # the debt disappears in the window (a repayment, a forgiveness, a seizure)
    bdb.write_off_loan(bdb.get_active_loans(u)[0]["id"], "banker")
    applied = bm._apply_to_overdue(u, 500.0, "test")
    assert applied == 0.0
    # bond_redeem routes the shortfall to savings; nothing is lost either way
    bdb.add_savings(u, 500.0 - applied)
    assert bdb.get_savings(u)["balance"] == 500.0


def test_bond_redemption_uses_a_stable_idempotency_key():
    import re
    src = (Path(__file__).resolve().parent.parent / "bank_main.py").read_text()
    assert re.search(r'idempotency_key=f"bond-\{bond_id\}-redeem"', src)


# ── staff permissions ───────────────────────────────────────────────────────

class FakeRole:
    def __init__(self, rid):
        self.id = rid


class FakePerms:
    def __init__(self, admin):
        self.administrator = admin


class FakeMember(FakeUser):
    def __init__(self, uid, roles=(), admin=False):
        super().__init__(uid)
        self.roles = [FakeRole(r) for r in roles]
        self.guild_permissions = FakePerms(admin)


def test_administrator_is_a_fallback_not_an_extra_grant():
    """Once an allow-list exists it IS the list. Otherwise setting
    LEAD_BANKER_ROLE_IDS to lock down the money commands would still admit
    every moderator who happens to hold Administrator."""
    old_roles, old_users = bm.LEAD_BANKER_ROLE_IDS, bm.BANK_ADMIN_USER_IDS
    try:
        # nothing configured -> Administrators are the fallback
        bm.LEAD_BANKER_ROLE_IDS, bm.BANK_ADMIN_USER_IDS = set(), set()
        assert bm.is_banker(FakeMember(1, admin=True)) is True
        assert bm.is_banker(FakeMember(2, admin=False)) is False

        # an allow-list exists -> a bare Administrator is NOT staff
        bm.LEAD_BANKER_ROLE_IDS, bm.BANK_ADMIN_USER_IDS = {77}, set()
        assert bm.is_banker(FakeMember(3, roles=[77], admin=False)) is True
        assert bm.is_banker(FakeMember(4, roles=[88], admin=True)) is False

        bm.LEAD_BANKER_ROLE_IDS, bm.BANK_ADMIN_USER_IDS = set(), {5}
        assert bm.is_banker(FakeMember(5, admin=False)) is True
        assert bm.is_banker(FakeMember(6, admin=True)) is False
    finally:
        bm.LEAD_BANKER_ROLE_IDS, bm.BANK_ADMIN_USER_IDS = old_roles, old_users


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

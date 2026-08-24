"""
bank_main.py - standalone Banking discord.py bot: /bank, /loan, /savings, /invest
on top of Restocker. Keeps savings/loans/ledger in bank.db and reaches the coin
wallet + stock exchange via Restocker's /api/v1/bank/* API. Interest accrues from
real elapsed time.
"""

from __future__ import annotations

import os
import math
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import abex_embed as ab          # the house embed builder
import bank_db as bdb
from restocker_client import RestockerClient, RestockerError, EXPECTED_API_VERSION


try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bank_main")

DISCORD_TOKEN = os.getenv("BANK_DISCORD_TOKEN", "").strip()
RESTOCKER_API_URL = os.getenv("RESTOCKER_API_URL", "").strip()
RESTOCKER_BANK_TOKEN = os.getenv("RESTOCKER_BANK_TOKEN", "").strip()

GUILD_ID = os.getenv("BANK_GUILD_ID", "").strip()

NEW_ACCOUNT_CHANNEL_ID = os.getenv("NEW_ACCOUNT_CHANNEL_ID", "1518146924270587934").strip()

LOAN_PROPOSALS_CHANNEL_ID = os.getenv("LOAN_PROPOSALS_CHANNEL_ID", "1515925123159556111").strip()

BOT_LOG_CHANNEL_ID = os.getenv("BOT_LOG_CHANNEL_ID", "1515925132051349617").strip()

SAVINGS_APR = float(os.getenv("SAVINGS_APR", "0.05"))
LOAN_APR = float(os.getenv("LOAN_APR", "0.18"))
LOAN_OVERDUE_EXTRA_APR = float(os.getenv("LOAN_OVERDUE_EXTRA_APR", "0.18"))
MAX_LOAN = int(os.getenv("MAX_LOAN", "100000"))
DEFAULT_LOAN_DAYS = int(os.getenv("DEFAULT_LOAN_DAYS", "30"))


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_ids(name: str) -> set[int]:
    """Parse a comma-separated list of Discord IDs."""
    out = set()
    for part in (os.getenv(name, "") or "").replace(" ", "").split(","):
        if part:
            try:
                out.add(int(part))
            except ValueError:
                log.warning("Ignoring non-numeric ID in %s: %r", name, part)
    return out


# Staff: anyone holding one of these roles, or listed by user ID, can use /admin.
LEAD_BANKER_ROLE_IDS = _env_ids("LEAD_BANKER_ROLE_IDS")
BANK_ADMIN_USER_IDS = _env_ids("BANK_ADMIN_USER_IDS")

# Loan approval gate
LOAN_REQUIRE_APPROVAL = _env_bool("LOAN_REQUIRE_APPROVAL", "1")
# Credit limit = base + (per-repaid-loan bonus x clean repayments), capped at
# MAX_LOAN. A per-user override in accounts.credit_limit beats all of this.
BASE_CREDIT_LIMIT = int(os.getenv("BASE_CREDIT_LIMIT", "10000"))
CREDIT_PER_REPAID_LOAN = int(os.getenv("CREDIT_PER_REPAID_LOAN", "5000"))
CREDIT_LATE_PENALTY = int(os.getenv("CREDIT_LATE_PENALTY", "5000"))
MAX_PENDING_LOANS = int(os.getenv("MAX_PENDING_LOANS", "1"))

# Collections
COLLECT_FROM_SAVINGS = _env_bool("COLLECT_FROM_SAVINGS", "1")
COLLECT_GRACE_DAYS = float(os.getenv("COLLECT_GRACE_DAYS", "3"))
GARNISH_BOND_PAYOUTS = _env_bool("GARNISH_BOND_PAYOUTS", "1")
OVERDUE_ANNOUNCE = _env_bool("OVERDUE_ANNOUNCE", "1")


def _parse_bond_terms(raw: str) -> dict[int, float]:
    """Parse BOND_TERMS='7:0.06,30:0.09,90:0.14' -> {7:0.06, 30:0.09, 90:0.14}."""
    out: dict[int, float] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            d, r = part.split(":")
            out[int(d.strip())] = float(r.strip())
        except ValueError:
            log.warning("Ignoring malformed BOND_TERMS entry: %r", part)
    return dict(sorted(out.items()))


BOND_TERMS = _parse_bond_terms(os.getenv("BOND_TERMS", "7:0.06,30:0.09,90:0.14"))
BOND_EARLY_PENALTY_PCT = float(os.getenv("BOND_EARLY_PENALTY_PCT", "0.0"))

COIN = "🪙"


def _bond_payout(principal: int, apr: float, term_days: int) -> int:
    """Fixed simple-interest payout at maturity, rounded to whole coins."""
    return int(round(principal * (1.0 + apr * term_days / 365.0)))

client_rs = RestockerClient(RESTOCKER_API_URL, RESTOCKER_BANK_TOKEN) if (RESTOCKER_API_URL and RESTOCKER_BANK_TOKEN) else None

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


_user_locks: dict[str, asyncio.Lock] = {}


def _user_lock(user_id) -> asyncio.Lock:
    key = str(user_id)
    lk = _user_locks.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _user_locks[key] = lk
    return lk



def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso(s):
    """Parse a stored ISO timestamp to an aware UTC datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_MIN_ACCRUAL = 0.0001


def fmt(n: float) -> str:
    return f"{n:,.0f}"



async def ensure_account(interaction: discord.Interaction, *, write: bool = True) -> bool:
    """Return True if the user has an ACTIVE account (opted_in); otherwise
    prompt and return False. A closed account (opted_in=0) still has a row,
    so this checks the flag, not just existence.

    write=True (the default) also rejects FROZEN accounts. Read-only commands
    pass write=False so a frozen user can still see where they stand."""
    acct = bdb.get_account(interaction.user.id)
    if not acct or not acct["opted_in"]:
        await interaction.response.send_message(
            "You don't have a bank account yet. Use `/bank open` first.", ephemeral=True
        )
        return False
    if write and acct.get("frozen"):
        reason = (acct.get("frozen_reason") or "").strip()
        await _reply(
            interaction,
            "Your account is frozen, so you can't move money right now."
            + (f" Reason: {reason}" if reason else "")
            + " Contact a Lead Banker.",
            error=True,
        )
        return False
    return True


def is_banker(user: discord.abc.User) -> bool:
    """Staff check for /admin: an allow-listed user ID, a Lead Banker role, or
    (as a fallback when neither is configured) guild Administrator."""
    if user.id in BANK_ADMIN_USER_IDS:
        return True
    roles = getattr(user, "roles", None)
    if roles and LEAD_BANKER_ROLE_IDS:
        if any(r.id in LEAD_BANKER_ROLE_IDS for r in roles):
            return True
    # Administrator is a FALLBACK, not an extra grant. Once an allow-list exists,
    # it is the whole list — otherwise setting LEAD_BANKER_ROLE_IDS to lock the
    # money commands down to Lead Bankers would silently still admit every
    # moderator who happens to hold Administrator.
    if LEAD_BANKER_ROLE_IDS or BANK_ADMIN_USER_IDS:
        return False
    perms = getattr(user, "guild_permissions", None)
    return bool(perms is not None and perms.administrator)



async def ensure_banker(interaction: discord.Interaction) -> bool:
    if is_banker(interaction.user):
        return True
    # A refusal is a plain ephemeral line, not a red embed.
    await _reply(interaction, "Lead Bankers only.", error=True)
    return False


def credit_limit_for(user_id) -> int:
    """How much total debt this user is allowed to carry.

    A per-user override (set by /admin creditlimit) wins outright. Otherwise it
    grows with a clean repayment record and shrinks for every loan that went
    overdue — so the limit is earned rather than flat."""
    acct = bdb.get_account(user_id) or {}
    override = acct.get("credit_limit")
    if override is not None:
        return max(0, int(override))
    h = bdb.loan_history(user_id)
    limit = (BASE_CREDIT_LIMIT
             + CREDIT_PER_REPAID_LOAN * h["repaid_count"]
             - CREDIT_LATE_PENALTY * h["late_count"])
    if h["written_off_count"]:
        # A written-off loan means the bank ate a loss on this person.
        limit = 0
    return max(0, min(limit, MAX_LOAN))



def _embed(title: str, desc: str = "", color: int = ab.ACCENT) -> discord.Embed:
    """Thin shim onto the house embed so every existing call site keeps working.

    The bar carries one meaning, not a per-command colour: `ab.ACCENT` for
    anything the bank asserts, `ab.GAIN` only where coins actually reach the
    reader, `ab.LOSS` for a failure or a loss to them, `ab.NEUTRAL` for the
    merely informational."""
    return ab.embed(title=title, desc=desc, colour=color)



async def _safe(interaction: discord.Interaction, coro):
    """Run a client coroutine, surfacing RestockerError as an ephemeral message.
    Returns the result, or None if it failed (message already sent)."""
    if client_rs is None:
        await _reply(interaction, "The bank isn't connected to Restocker (missing config).",
                     error=True)
        return None
    try:
        return await coro
    except RestockerError as e:
        if e.code == "insufficient":
            await _reply(interaction, "Not enough coins in your wallet for that.", error=True)
        else:
            await _reply(interaction, f"Restocker error: {e}", error=True)
        return None


async def _reply(interaction: discord.Interaction, content=None, *, embed=None, error=False):
    """Reply whether or not we've already responded/deferred."""
    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if interaction.response.is_done():
        await interaction.followup.send(ephemeral=error, **kwargs)
    else:
        await interaction.response.send_message(ephemeral=error, **kwargs)


async def _get_channel(channel_id: str):
    """Resolve a channel ID to a channel object, using the cache first."""
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        channel = await bot.fetch_channel(int(channel_id))
    return channel



async def _post_new_account_ticket(member: discord.abc.User) -> None:
    """Post a 'new account' ticket embed to NEW_ACCOUNT_CHANNEL_ID for Lead
    Bankers to review, with reactions to mark it approved/denied.

    Best-effort and fire-and-forget: this never raises into the caller, so a
    missing channel/permission can't break /bank open for the user opening
    the account. Failures are only logged.
    """
    if not NEW_ACCOUNT_CHANNEL_ID:
        return
    try:
        channel = await _get_channel(NEW_ACCOUNT_CHANNEL_ID)
        embed = ab.embed(
            title="New account, pending review",
            kicker="/bank open",
            desc=f"{member.mention} (`{member.id}`) opened a bank account.",
            groups=[("Account", ab.rows([("Member", member.mention),
                                         ("Discord id", f"`{member.id}`"),
                                         ("Opened", ab.when(utcnow()))]))],
            colour=ab.ACCENT,
        )
        msg = await channel.send(embed=embed)
        # These two reactions are the review affordance bankers click; they are
        # message reactions, not embed copy, and Discord has no non-emoji form.
        await msg.add_reaction("\u2705")
        await msg.add_reaction("\u274c")
    except Exception:
        log.exception("Failed to post new-account ticket (channel %s) for user %s",
                      NEW_ACCOUNT_CHANNEL_ID, member.id)



def _loan_proposal_embed(member: discord.abc.User, loan: dict, days: int,
                         history: dict, limit: int, existing_debt: float) -> discord.Embed:
    """The card Lead Bankers actually decide from — the request plus the
    borrower's track record, so the decision doesn't need a second lookup."""
    return ab.embed(
        title=f"Loan request #{loan['id']}",
        kicker="/loan request",
        desc=(f"{member.mention} (`{member.id}`) wants {ab.coins(loan['principal'])} "
              f"over a {days} day term at {LOAN_APR*100:.1f}% APR."),
        band=[("Requested", ab.coins(loan["principal"])),
              ("Term", f"{days} days"),
              ("Existing debt", ab.coins(existing_debt))],
        groups=[("Standing", ab.rows([
                    ("Credit limit", ab.coins(limit)),
                    ("Headroom", ab.coins(max(0.0, limit - existing_debt))),
                ], strong="Credit limit")),
                ("Track record", ab.rows([
                    ("Loans repaid", history["repaid_count"]),
                    ("Times late", history["late_count"]),
                    ("Written off", history["written_off_count"]),
                    ("Previously denied", history["denied_count"]),
                ]))],
        foot="No coins have moved yet. Approve to disburse.",
        colour=ab.ACCENT,
    )



async def _post_loan_proposal(member: discord.abc.User, loan: dict, days: int,
                              *, pending: bool) -> None:
    """Post a loan to LOAN_PROPOSALS_CHANNEL_ID.

    pending=True attaches live Approve/Deny buttons and NO coins have moved yet.
    pending=False is the legacy record-only post used when LOAN_REQUIRE_APPROVAL
    is off and the loan already disbursed.

    Best-effort/fire-and-forget — failures are only logged. If the post fails
    while the loan is pending, the loan is still approvable via
    `/admin loans` + `/admin approve`, so a broken channel can't strand it."""
    if not LOAN_PROPOSALS_CHANNEL_ID:
        return
    try:
        channel = await _get_channel(LOAN_PROPOSALS_CHANNEL_ID)
        if pending:
            history = bdb.loan_history(member.id)
            embed = _loan_proposal_embed(member, loan, days, history,
                                         credit_limit_for(member.id),
                                         bdb.total_debt(member.id))
            await channel.send(embed=embed, view=LoanDecisionView(loan["id"]))
        else:
            embed = ab.embed(
                title=f"Loan issued #{loan['id']}",
                desc=(f"{member.mention} (`{member.id}`) borrowed "
                      f"{ab.coins(loan['principal'])}."),
                band=[("Principal", ab.coins(loan["principal"])),
                      ("Term", f"{days} days"),
                      ("APR", f"{LOAN_APR*100:.1f}%")],
                colour=ab.ACCENT,
            )
            await channel.send(embed=embed)
    except Exception:
        log.exception("Failed to post loan proposal (channel %s) for user %s",
                      LOAN_PROPOSALS_CHANNEL_ID, member.id)


class LoanDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bank:loan:(?P<action>approve|deny):(?P<loan_id>\d+)",
):
    """Approve/Deny on a loan proposal.

    Built as a DynamicItem so the loan ID rides inside the button's custom_id.
    That makes the buttons survive a bot restart: discord.py rebuilds the button
    from the ID in the click rather than needing the original View object to
    still be in memory. A proposal posted on Monday is still clickable after
    Friday's redeploy.
    """


    def __init__(self, action: str, loan_id: int):
        self.action = action
        self.loan_id = int(loan_id)
        # No emoji on the label, and the custom_id is untouched — that string is
        # what rebuilds this button after a restart, so proposals posted before
        # today keep working.
        super().__init__(
            discord.ui.Button(
                label="Approve" if action == "approve" else "Deny",
                style=discord.ButtonStyle.success if action == "approve" else discord.ButtonStyle.danger,
                custom_id=f"bank:loan:{action}:{loan_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], int(match["loan_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_banker(interaction):
            return
        if self.action == "approve":
            await approve_loan(interaction, self.loan_id)
        else:
            await deny_loan(interaction, self.loan_id)


class LoanDecisionView(discord.ui.View):
    def __init__(self, loan_id: int):
        super().__init__(timeout=None)
        self.add_item(LoanDecisionButton("approve", loan_id))
        self.add_item(LoanDecisionButton("deny", loan_id))



async def _stamp_proposal(interaction: discord.Interaction, title: str,
                          note: str, color: int) -> None:
    """Rewrite the proposal message with the verdict and strip its buttons, so
    the channel shows what was decided and nobody can click it twice.

    Only the title, bar and footer change; the request's own fields stay, which
    is what makes the stamped card still readable as a record. Nothing parses
    these strings — the loan id rides in the button custom_id, not the title."""
    msg = getattr(interaction, "message", None)
    if msg is None:
        return
    try:
        embed = msg.embeds[0] if msg.embeds else ab.embed(title=title, colour=color)
        embed.title = title
        embed.colour = discord.Colour(color)
        embed.set_footer(text=note)
        await msg.edit(embed=embed, view=None)
    except Exception:
        log.exception("Failed to stamp loan proposal message %s", getattr(msg, "id", "?"))



async def approve_loan(interaction: discord.Interaction, loan_id: int) -> None:
    """Disburse a pending loan. Safe to race: the pending->approving claim is a
    conditional UPDATE, so of two bankers clicking Approve at the same instant
    exactly one disburses."""
    await interaction.response.defer(ephemeral=True)
    loan = bdb.get_loan(loan_id)
    if not loan:
        await interaction.followup.send("No such loan.", ephemeral=True)
        return
    if loan["status"] != "pending":
        await interaction.followup.send(
            f"Loan #{loan_id} is already {loan['status']} — nothing to approve.",
            ephemeral=True)
        return

    borrower_id = loan["user_id"]

    # The whole check→claim→disburse→finalize sequence runs under the borrower's
    # lock. Without it, two proposals for the same person approved at the same
    # moment would both read the pre-disbursement debt (total_debt only counts
    # 'active' loans) and both pass the credit check — busting the limit.
    async with _user_lock(borrower_id):
        loan = bdb.get_loan(loan_id)
        if not loan or loan["status"] != "pending":
            await interaction.followup.send(
                "Someone else just decided that loan.", ephemeral=True)
            return

        acct = bdb.get_account(borrower_id) or {}
        if not acct.get("opted_in"):
            await interaction.followup.send(
                f"Loan #{loan_id}: the borrower's account is closed.", ephemeral=True)
            return
        if acct.get("frozen"):
            await interaction.followup.send(
                f"Loan #{loan_id}: the borrower's account is frozen. Unfreeze it first.",
                ephemeral=True)
            return

        # Re-check the limit at approval time — debt can have grown since the request.
        principal = float(loan["principal"])
        limit = credit_limit_for(borrower_id)
        debt = bdb.total_debt(borrower_id)
        if debt + principal > limit:
            await interaction.followup.send(
                f"Loan #{loan_id} would put them at {ab.coins(debt + principal)} against a "
                f"{ab.coins(limit)} limit. Raise it with `/admin creditlimit` or deny.",
                ephemeral=True)
            return

        if not bdb.claim_pending_loan(loan_id, interaction.user.id):
            await interaction.followup.send("Someone else just decided that loan.", ephemeral=True)
            return

        days = int(loan["term_days"] or DEFAULT_LOAN_DAYS)
        try:
            # A FIXED idempotency key, not a fresh uuid: if the credit lands on
            # Restocker but the response is lost, this loan is released back to
            # pending and someone clicks Approve again — the retry must be
            # recognised as the same disbursement, or the borrower is paid twice.
            res = await _safe(interaction, client_rs.adjust(
                borrower_id, int(principal), reason=f"loan #{loan_id} disbursement",
                count_principal=False, idempotency_key=f"loan-{loan_id}-disburse"))
            if res is None:
                bdb.release_pending_loan(loan_id)
                return
            due = (utcnow() + timedelta(days=days)).isoformat()
            bdb.finalize_loan_approval(loan_id, due)
        except Exception:
            # Anything at all — a dead interaction token, a Discord 5xx while
            # reporting the error — must not strand the loan in 'approving',
            # where no command can see it and only manual SQL could free it.
            bdb.release_pending_loan(loan_id)
            log.exception("Approval of loan #%s failed after claim; released to pending", loan_id)
            raise

    bdb.log(borrower_id, "loan_out", principal, f"loan #{loan_id} {days}d approved by {interaction.user.id}")

    due_dt = _parse_iso(due)
    await interaction.followup.send(
        f"Approved loan #{loan_id} — {ab.coins(principal)} disbursed over a {days} day term.",
        ephemeral=True)
    await _stamp_proposal(
        interaction, f"Loan approved #{loan_id}",
        f"Approved by {interaction.user.display_name} · {days} day term, due {due[:10]}",
        ab.ACCENT)
    asyncio.create_task(_log_activity(ab.line(
        "Loan approved", f"#{loan_id}", ab.coins(principal), f"to <@{borrower_id}>",
        f"{days} day term", f"due {ab.when(due_dt)}" if due_dt else "",
        f"approved by {interaction.user.mention}")))



async def deny_loan(interaction: discord.Interaction, loan_id: int) -> None:
    await interaction.response.defer(ephemeral=True)
    loan = bdb.get_loan(loan_id)
    if not loan:
        await interaction.followup.send("No such loan.", ephemeral=True)
        return
    if loan["status"] != "pending":
        await interaction.followup.send(
            f"Loan #{loan_id} is already {loan['status']}.", ephemeral=True)
        return
    if not bdb.deny_loan(loan_id, interaction.user.id):
        await interaction.followup.send("Someone else just decided that loan.", ephemeral=True)
        return
    await interaction.followup.send(f"Denied loan #{loan_id}.", ephemeral=True)
    await _stamp_proposal(interaction, f"Loan denied #{loan_id}",
                          f"Denied by {interaction.user.display_name}", ab.LOSS)
    asyncio.create_task(_log_activity(ab.line(
        "Loan denied", f"#{loan_id}", f"for <@{loan['user_id']}>",
        f"denied by {interaction.user.mention}")))



async def _log_activity(text: str) -> None:
    """Post one audit-trail line to BOT_LOG_CHANNEL_ID. One event, one line, no
    embed — the compact feed shape. Best-effort/fire-and-forget: failures are
    only logged, never surfaced to the user."""
    if not BOT_LOG_CHANNEL_ID:
        return
    try:
        channel = await _get_channel(BOT_LOG_CHANNEL_ID)
        await channel.send(text)
    except Exception:
        log.exception("Failed to post bot-log line (channel %s)", BOT_LOG_CHANNEL_ID)



bank_group = app_commands.Group(name="bank", description="Your bank account")


@bank_group.command(name="open", description="Open a bank account")

async def bank_open(interaction: discord.Interaction):
    is_new = bdb.get_account(interaction.user.id) is None
    bdb.open_account(interaction.user.id, interaction.user.display_name)
    if is_new:
        # One fact, one line — a fresh account does not need an embed.
        msg = ("Your bank account is open. Try `/bank deposit`, `/loan request`, "
               "or `/invest list`.")
    else:
        msg = "Welcome back — your account is active."
    await interaction.response.send_message(msg, ephemeral=True)
    if is_new:
        asyncio.create_task(_post_new_account_ticket(interaction.user))


@bank_group.command(name="balance", description="See your wallet, savings, and debt")

async def bank_balance(interaction: discord.Interaction):
    if not await ensure_account(interaction, write=False):
        return
    await interaction.response.defer(ephemeral=True)
    wallet = await _safe(interaction, client_rs.get_balance(interaction.user.id))
    if wallet is None:
        return
    sav = bdb.get_savings(interaction.user.id)["balance"]
    debt = bdb.total_debt(interaction.user.id)
    bonds = bdb.total_bonds_value(interaction.user.id)
    net = wallet["coins"] + sav + bonds - debt
    e = ab.embed(
        title="Your bank",
        kicker="/bank balance",
        band=[("Wallet", ab.coins(wallet["coins"])),
              ("Savings", ab.coins(sav)),
              ("Debt", ab.coins(debt))],
        groups=[("Position", ab.rows([
            ("Bonds, value at maturity", ab.coins(bonds)),
            ("Net worth", ab.coins(net)),
        ], strong="Net worth"))],
        foot=f"Savings APR {SAVINGS_APR*100:.1f}% · Loan APR {LOAN_APR*100:.1f}%",
        colour=ab.ACCENT,
    )
    await interaction.followup.send(embed=e, ephemeral=True)


@bank_group.command(name="deposit", description="Move coins from your wallet into savings")
@app_commands.describe(amount="How many coins to deposit")

async def bank_deposit(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100_000_000]):
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    res = await _safe(interaction, client_rs.adjust(interaction.user.id, -amount, reason="bank deposit"))
    if res is None:
        return
    new_sav = bdb.add_savings(interaction.user.id, amount)
    bdb.log(interaction.user.id, "deposit", amount, "wallet->savings")
    await interaction.followup.send(
        embed=ab.embed(title="Deposit complete",
                       kicker="/bank deposit",
                       desc=f"Moved {ab.coins(amount)} from your wallet into savings.",
                       band=[("Savings", ab.coins(new_sav)),
                             ("Wallet", ab.coins(res["coins"]))],
                       colour=ab.ACCENT),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(ab.line(
        "Deposit", interaction.user.mention, ab.coins(amount), "into savings")))


@bank_group.command(name="withdraw", description="Move coins from savings back to your wallet")
@app_commands.describe(amount="How many coins to withdraw")

async def bank_withdraw(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100_000_000]):
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    if not bdb.try_debit_savings(interaction.user.id, amount):
        sav = bdb.get_savings(interaction.user.id)["balance"]
        await interaction.followup.send(
            f"You only have {ab.coins(sav)} in savings.", ephemeral=True)
        return
    res = await _safe(interaction, client_rs.adjust(interaction.user.id, amount, reason="bank withdraw"))
    if res is None:
        bdb.add_savings(interaction.user.id, amount)
        return
    new_sav = bdb.get_savings(interaction.user.id)["balance"]
    bdb.log(interaction.user.id, "withdraw", amount, "savings->wallet")
    await interaction.followup.send(
        # Green: coins actually reached the reader's wallet.
        embed=ab.embed(title="Withdrawal complete",
                       kicker="/bank withdraw",
                       desc=f"Moved {ab.coins(amount)} from savings to your wallet.",
                       band=[("Savings", ab.coins(new_sav)),
                             ("Wallet", ab.coins(res["coins"]))],
                       colour=ab.GAIN),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(ab.line(
        "Withdrawal", interaction.user.mention, ab.coins(amount), "from savings")))


@bank_group.command(name="transfer", description="Send coins from your wallet to another member")
@app_commands.describe(member="Who to pay", amount="How many coins")

async def bank_transfer(interaction: discord.Interaction, member: discord.Member,
                        amount: app_commands.Range[int, 1, 100_000_000]):
    if not await ensure_account(interaction):
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("You can't pay yourself.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("You can't pay a bot.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    res = await _safe(interaction, client_rs.transfer(interaction.user.id, member.id, amount,
                                                      reason=f"transfer to {member.display_name}"))
    if res is None:
        return
    bdb.log(interaction.user.id, "transfer_out", amount, f"to {member.id}")
    bdb.log(str(member.id), "transfer_in", amount, f"from {interaction.user.id}")
    await interaction.followup.send(
        embed=ab.embed(title="Payment sent",
                       kicker="/bank transfer",
                       desc=f"Sent {ab.coins(amount)} to {member.mention}.",
                       band=[("Your wallet", ab.coins(res["from"]["coins"]))],
                       colour=ab.ACCENT),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(ab.line(
        "Payment", f"{interaction.user.mention} to {member.mention}", ab.coins(amount))))



def _kind_words(kind) -> str:
    """`transfer_in` -> `transfer in`. A ledger kind is an internal id."""
    return str(kind or "").replace("_", " ").strip() or "entry"


@bank_group.command(name="history", description="Your recent bank activity")

async def bank_history(interaction: discord.Interaction):
    if not await ensure_account(interaction, write=False):
        return
    rows = bdb.recent_ledger(interaction.user.id, 12)
    if not rows:
        await interaction.response.send_message("No activity yet.", ephemeral=True)
        return
    # Entries that record a state change rather than a movement of coins.
    NON_MONETARY = ("account_closed", "account_frozen", "account_unfrozen", "credit_limit_set")
    GAINS = ("withdraw", "transfer_in", "loan_out", "interest_savings",
             "stock_sell", "bond_redeem", "loan_written_off")
    lines = []
    for r in rows:
        ts = r["ts"][:16].replace("T", " ")
        if r["kind"] in NON_MONETARY:
            lines.append(ab.line(ts, _kind_words(r["kind"])))
            continue
        if r["kind"] == "admin_savings_adjust":
            sign = 1 if r["amount"] >= 0 else -1   # the amount itself carries the direction
        else:
            sign = 1 if r["kind"] in GAINS else -1
        lines.append(ab.line(ts, _kind_words(r["kind"]),
                             ab.signed(sign * abs(r["amount"]))))
    # One line per event, the shape John accepted for feeds. The padded monospace
    # block this replaced is the thing `abex_embed`'s own docstring rejects: it
    # wraps at 380px, and a column of :<20 padding is a table drawn by hand.
    body = "\n".join(lines)[:3800]
    await interaction.response.send_message(
        embed=ab.embed(title="Recent activity", kicker="/bank history",
                       desc=body, colour=ab.NEUTRAL),
        ephemeral=True)


class _CloseAccountConfirm(discord.ui.View):
    """Yes/No confirmation for /bank close. Only the requesting user can press
    a button. Closing moves money (savings payout), so we don't act on it
    without an explicit click."""

    def __init__(self, user_id: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.confirmed: bool = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Close my account", style=discord.ButtonStyle.danger)

    @discord.ui.button(label="Close my account", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(content="Closing your account…", embed=None, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.edit_message(content="Cancelled — your account is unchanged.",
                                                 embed=None, view=None)

    async def on_timeout(self) -> None:
        self.confirmed = False


@bank_group.command(name="close", description="Close (delete) your bank account")

async def bank_close(interaction: discord.Interaction):
    if not await ensure_account(interaction):
        return
    debt = bdb.total_debt(interaction.user.id)
    if debt > 0:
        await interaction.response.send_message(
            f"You still owe {ab.coins(debt)}. Repay with `/loan repay` before closing "
            f"your account.", ephemeral=True)
        return
    active_bonds = bdb.get_bonds(interaction.user.id, "active")
    if active_bonds:
        await interaction.response.send_message(
            f"You have {len(active_bonds)} active bond(s). Redeem them with `/bond redeem` "
            f"before closing your account.", ephemeral=True)
        return

    sav = bdb.get_savings(interaction.user.id)["balance"]
    cashout_note = (f"This moves {ab.coins(sav)} from savings to your wallet and "
                    if sav > 0 else "This ")
    view = _CloseAccountConfirm(interaction.user.id)
    await interaction.response.send_message(
        embed=ab.embed(title="Close your bank account?",
                       kicker="/bank close",
                       desc=(f"{cashout_note}deactivates your bank account. Your history "
                             f"isn't deleted — `/bank open` reopens it any time."),
                       colour=ab.LOSS),
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if not view.confirmed:
        return

    if sav > 0:
        res = await _safe(interaction, client_rs.adjust(
            interaction.user.id, sav, reason="bank account closed — savings cashed out"))
        if res is None:
            return
        bdb.add_savings(interaction.user.id, -sav)
        bdb.log(interaction.user.id, "withdraw", sav, "savings->wallet (account closed)")

    bdb.close_account(interaction.user.id)
    bdb.log(interaction.user.id, "account_closed", 0, "")
    await interaction.followup.send(
        "Your bank account is now closed."
        + (f" {ab.coins(sav)} was moved to your wallet." if sav > 0 else ""),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(ab.line(
        "Account closed", interaction.user.mention,
        f"cashed out {ab.coins(sav)} from savings" if sav > 0 else "")))



loan_group = app_commands.Group(name="loan", description="Borrow and repay coins")


@loan_group.command(name="request", description="Borrow coins (credited to your wallet)")
@app_commands.describe(amount="How many coins to borrow", days="Term in days (default 30)")
async def loan_request(interaction: discord.Interaction,
                       amount: app_commands.Range[int, 1, 100_000_000],
                       days: app_commands.Range[int, 1, 365] = DEFAULT_LOAN_DAYS):
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    # Under the user's lock so two /loan request spammed at once can't both see
    # an empty queue and both slip past MAX_PENDING_LOANS.
    async with _user_lock(interaction.user.id):
        return await _do_loan_request(interaction, amount, days)



async def _do_loan_request(interaction: discord.Interaction, amount: int, days: int):
    current_debt = bdb.total_debt(interaction.user.id)
    limit = credit_limit_for(interaction.user.id)
    if limit <= 0:
        await interaction.followup.send(
            "Your credit limit is 0 — the bank isn't lending to you right now. "
            "Talk to a Lead Banker.", ephemeral=True)
        return
    if current_debt + amount > limit:
        await interaction.followup.send(
            f"That would put your debt at {ab.coins(current_debt + amount)}, over your "
            f"{ab.coins(limit)} credit limit (current debt {ab.coins(current_debt)}). "
            f"Your limit grows as you repay loans on time.", ephemeral=True)
        return

    pending = bdb.get_pending_loans(interaction.user.id)
    if LOAN_REQUIRE_APPROVAL and len(pending) >= MAX_PENDING_LOANS:
        await interaction.followup.send(
            f"You already have {len(pending)} loan request awaiting approval "
            f"(#{pending[0]['id']}). Wait for a decision first.", ephemeral=True)
        return

    if LOAN_REQUIRE_APPROVAL:
        # No coins move here. The loan sits at status='pending' — not counted as
        # debt, no interest — until a Lead Banker approves it.
        loan = bdb.create_loan(interaction.user.id, float(amount), LOAN_APR, None,
                               status="pending", term_days=days)
        await interaction.followup.send(
            embed=ab.embed(title=f"Loan request #{loan['id']} submitted",
                           kicker="/loan request",
                           desc=(f"Requested {ab.coins(amount)} over a {days} day term. "
                                 f"A Lead Banker has to approve it — nothing has been paid "
                                 f"out yet. Check with `/loan status`."),
                           band=[("Requested", ab.coins(amount)),
                                 ("Term", f"{days} days"),
                                 ("APR", f"{LOAN_APR*100:.1f}%")],
                           colour=ab.NEUTRAL),
            ephemeral=True,
        )
        asyncio.create_task(_post_loan_proposal(interaction.user, loan, days, pending=True))
        return

    res = await _safe(interaction, client_rs.adjust(interaction.user.id, amount,
                                                    reason="loan disbursement", count_principal=False))
    if res is None:
        return
    due = (utcnow() + timedelta(days=days)).isoformat()
    loan = bdb.create_loan(interaction.user.id, float(amount), LOAN_APR, due, term_days=days)
    bdb.log(interaction.user.id, "loan_out", amount, f"loan #{loan['id']} {days}d")
    due_dt = _parse_iso(due)
    await interaction.followup.send(
        # Green: the principal has actually reached their wallet.
        embed=ab.embed(title=f"Loan approved #{loan['id']}",
                       kicker="/loan request",
                       desc=(f"Borrowed {ab.coins(amount)} over a {days} day term at "
                             f"{LOAN_APR*100:.1f}% APR. Repay with `/loan repay`."),
                       band=[("Principal", ab.coins(amount)),
                             ("Term", f"{days} days"),
                             ("Wallet", ab.coins(res["coins"]))],
                       groups=[("Repayment", ab.rows([
                           ("Due", ab.when(due_dt) if due_dt else due[:10]),
                           ("APR", f"{LOAN_APR*100:.1f}%"),
                       ]))],
                       colour=ab.GAIN),
        ephemeral=True,
    )
    asyncio.create_task(_post_loan_proposal(interaction.user, loan, days, pending=False))


@loan_group.command(name="repay", description="Repay your loans from your wallet")
@app_commands.describe(amount="How many coins to repay")

async def loan_repay(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100_000_000]):
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    async with _user_lock(interaction.user.id):
        loans = bdb.get_active_loans(interaction.user.id)
        if not loans:
            await interaction.followup.send("You have no active loans.", ephemeral=True)
            return
        debt = sum(float(l["balance"]) for l in loans)
        pay = min(amount, math.ceil(debt))
        res = await _safe(interaction, client_rs.adjust(interaction.user.id, -pay, reason="loan repayment"))
        if res is None:
            return
        remaining = pay
        for l in loans:
            if remaining <= 0:
                break
            chunk = min(remaining, float(l["balance"]))
            bdb.apply_loan_payment(l["id"], chunk)
            remaining -= chunk
        bdb.log(interaction.user.id, "loan_repay", pay, "repayment")
        new_debt = bdb.total_debt(interaction.user.id)
    await interaction.followup.send(
        embed=ab.embed(title="Repayment applied",
                       kicker="/loan repay",
                       desc=f"Repaid {ab.coins(pay)} from your wallet.",
                       band=[("Remaining debt", ab.coins(new_debt)),
                             ("Wallet", ab.coins(res["coins"]))],
                       colour=ab.ACCENT),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(ab.line(
        "Repayment", interaction.user.mention, ab.coins(pay),
        f"remaining debt {ab.coins(new_debt)}")))


@loan_group.command(name="status", description="See your outstanding loans")

async def loan_status(interaction: discord.Interaction):
    if not await ensure_account(interaction, write=False):
        return
    loans = bdb.get_active_loans(interaction.user.id)
    pending = bdb.get_pending_loans(interaction.user.id)
    limit = credit_limit_for(interaction.user.id)
    if not loans and not pending:
        # Nothing outstanding is one fact: one plain line, no embed.
        await interaction.response.send_message(
            f"No active loans. You can borrow up to {ab.coins(limit)}.", ephemeral=True)
        return

    groups = []
    if pending:
        # Every loan figure states its term.
        groups.append(("Awaiting approval", ab.rows(
            [(f"#{p['id']} · {p['term_days']} day term", ab.coins(p["principal"]))
             for p in pending])))

    overdue_any = False
    active_rows = []
    for l in loans:
        due_raw = l["due_at"] or ""
        d = _parse_iso(due_raw)
        is_overdue = bool(d and utcnow() > d)
        overdue_any = overdue_any or is_overdue
        term = f"{l['term_days']} day term" if l.get("term_days") else "term not recorded"
        when_txt = ab.when(d) if d else (due_raw[:10] or "no due date")
        label = f"#{l['id']} · {term} · due {when_txt}" + (" · overdue" if is_overdue else "")
        active_rows.append((label,
                            f"{ab.coins(l['balance'])} owed of {ab.coins(l['principal'])} "
                            f"borrowed at {l['apr']*100:.1f}% APR"))
    if active_rows:
        groups.append(("Active loans", ab.rows(active_rows)))

    total = bdb.total_debt(interaction.user.id)
    foot = (f"Overdue loans accrue an extra {LOAN_OVERDUE_EXTRA_APR*100:.0f}% APR until repaid."
            if overdue_any else "")
    await interaction.response.send_message(
        embed=ab.embed(title="Your loans",
                       kicker="/loan status",
                       band=[("Total debt", ab.coins(total)),
                             ("Credit limit", ab.coins(limit)),
                             ("Headroom", ab.coins(max(0, limit - total)))],
                       groups=groups,
                       foot=foot,
                       colour=ab.LOSS if overdue_any else ab.ACCENT),
        ephemeral=True,
    )



savings_group = app_commands.Group(name="savings", description="Savings info")


@savings_group.command(name="rate", description="See current savings & loan rates")

async def savings_rate(interaction: discord.Interaction):
    daily = SAVINGS_APR / 365
    await interaction.response.send_message(
        embed=ab.embed(title="Rates",
                       kicker="/savings rate",
                       band=[("Savings APR", f"{SAVINGS_APR*100:.2f}%"),
                             ("Loan APR", f"{LOAN_APR*100:.2f}%"),
                             ("Daily on savings", f"{daily*100:.4f}%")],
                       foot="Compounded daily. Interest is applied once every 24 hours.",
                       colour=ab.NEUTRAL),
        ephemeral=True,
    )



bond_group = app_commands.Group(name="bond", description="Lock coins in fixed-term bonds for higher interest")



async def _term_autocomplete(interaction: discord.Interaction, current: str):
    out = []
    for days, apr in BOND_TERMS.items():
        out.append(app_commands.Choice(
            name=f"{days} day term — {apr*100:.1f}% APR", value=days))
    return out[:25]


@bond_group.command(name="rates", description="See available bond terms and rates")

async def bond_rates(interaction: discord.Interaction):
    if not BOND_TERMS:
        await interaction.response.send_message("No bond products are configured.", ephemeral=True)
        return
    rows = []
    for days, apr in BOND_TERMS.items():
        ex = _bond_payout(1000, apr, days)
        # Every bond figure states its term.
        rows.append((f"{days} day term",
                     f"{apr*100:.1f}% APR · {ab.coins(1000)} becomes {ab.coins(ex)} at maturity"))
    note = ("Early redemption returns your principal"
            + (f" minus a {BOND_EARLY_PENALTY_PCT*100:.0f}% penalty" if BOND_EARLY_PENALTY_PCT else "")
            + " but forfeits the interest.")
    await interaction.response.send_message(
        embed=ab.embed(title="Bond rates",
                       kicker="/bond rates",
                       groups=[("Terms", ab.rows(rows))],
                       foot=note,
                       colour=ab.NEUTRAL),
        ephemeral=True,
    )


@bond_group.command(name="buy", description="Lock coins into a fixed-term bond")
@app_commands.describe(amount="How many coins to lock", term="Bond term")
@app_commands.autocomplete(term=_term_autocomplete)

async def bond_buy(interaction: discord.Interaction,
                   amount: app_commands.Range[int, 1, 100_000_000],
                   term: int):
    if not await ensure_account(interaction):
        return
    if term not in BOND_TERMS:
        avail = ", ".join(f"{d} days" for d in BOND_TERMS) or "none"
        await interaction.response.send_message(
            f"{term} isn't an available term. Available: {avail}. See `/bond rates`.",
            ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    apr = BOND_TERMS[term]
    payout = _bond_payout(amount, apr, term)
    res = await _safe(interaction, client_rs.adjust(interaction.user.id, -amount, reason=f"bond {term}d"))
    if res is None:
        return
    matures = (utcnow() + timedelta(days=term)).isoformat()
    bond = bdb.create_bond(interaction.user.id, float(amount), apr, term, float(payout), matures)
    bdb.log(interaction.user.id, "bond_buy", amount, f"bond #{bond['id']} {term}d")
    matures_dt = _parse_iso(matures)
    await interaction.followup.send(
        embed=ab.embed(title=f"Bond bought #{bond['id']}",
                       kicker="/bond buy",
                       desc=(f"Locked {ab.coins(amount)} for a {term} day term at "
                             f"{apr*100:.1f}% APR."),
                       band=[("Term", f"{term} days"),
                             ("Pays out", ab.coins(payout)),
                             ("Wallet", ab.coins(res["coins"]))],
                       groups=[("Maturity", ab.rows([
                           ("Matures", ab.when(matures_dt) if matures_dt else matures[:10]),
                           ("Payout at maturity", ab.coins(payout)),
                       ], strong="Payout at maturity"))],
                       colour=ab.ACCENT),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(ab.line(
        "Bond bought", f"#{bond['id']}", interaction.user.mention, ab.coins(amount),
        f"{term} day term")))


@bond_group.command(name="list", description="See your bonds")

async def bond_list(interaction: discord.Interaction):
    if not await ensure_account(interaction, write=False):
        return
    bonds = bdb.get_bonds(interaction.user.id, "active")
    if not bonds:
        await interaction.response.send_message(
            "You have no active bonds. Buy one with `/bond buy`.", ephemeral=True)
        return
    now = utcnow()
    rows = []
    for b in bonds:
        matured = now.isoformat() >= b["matures_at"]
        m_dt = _parse_iso(b["matures_at"])
        when_txt = ("matured, redeem now" if matured
                    else f"matures {ab.when(m_dt) if m_dt else b['matures_at'][:10]}")
        rows.append((f"#{b['id']} · {b['term_days']} day term · {when_txt}",
                     f"{ab.coins(b['principal'])} at {b['apr']*100:.1f}% APR "
                     f"pays {ab.coins(b['payout'])}"))
    locked = bdb.total_bonds_value(interaction.user.id)
    await interaction.response.send_message(
        embed=ab.embed(title="Your bonds",
                       kicker="/bond list",
                       band=[("Value at maturity", ab.coins(locked)),
                             ("Bonds held", str(len(bonds)))],
                       groups=[("Bonds", ab.rows(rows))],
                       colour=ab.ACCENT),
        ephemeral=True,
    )


@bond_group.command(name="redeem", description="Redeem a bond (full payout if matured, principal if early)")
@app_commands.describe(bond_id="The bond number from /bond list")

async def bond_redeem(interaction: discord.Interaction, bond_id: int):
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    bond = bdb.get_bond(bond_id)
    if not bond or str(bond["user_id"]) != str(interaction.user.id):
        await interaction.followup.send("That bond isn't yours or doesn't exist.", ephemeral=True)
        return
    if bond["status"] != "active":
        await interaction.followup.send("That bond has already been redeemed.", ephemeral=True)
        return

    now = utcnow()
    matured = now.isoformat() >= bond["matures_at"]
    if matured:
        amount = int(round(bond["payout"]))
        kind_note = "matured payout"
    else:
        principal = int(round(bond["principal"]))
        penalty = int(round(principal * BOND_EARLY_PENALTY_PCT))
        amount = max(0, principal - penalty)
        kind_note = ("early redemption, interest forfeited"
                     + (f", {ab.coins(penalty)} penalty" if penalty else ""))

    # Everything below runs under the user's lock. The split between "goes to
    # debt" and "goes to the wallet" is decided before an HTTP round trip and
    # acted on after it; without the lock, a collections pass or a /loan repay
    # landing in that window could clear the debt, leaving the garnished share
    # applied to nothing and simply destroyed.
    async with _user_lock(interaction.user.id):
        if bdb.get_bond(bond_id)["status"] != "active":
            await interaction.followup.send("That bond has already been redeemed.", ephemeral=True)
            return

        # A bond payout is money the bank is already holding, so overdue debt is
        # settled out of it before the rest reaches the wallet. Only OVERDUE debt
        # is garnished — a loan that's merely outstanding is left alone.
        garnish = 0
        if GARNISH_BOND_PAYOUTS:
            garnish = int(min(amount, math.floor(_overdue_debt(interaction.user.id))))
        to_wallet = amount - garnish

        if not bdb.claim_bond(bond_id):
            await interaction.followup.send("That bond has already been redeemed.", ephemeral=True)
            return

        res = None
        if to_wallet > 0:
            # Fixed idempotency key: if this credit lands but the response is
            # lost, the bond is unclaimed and redeemable again — the retry has
            # to be recognised as the same payout rather than paid twice.
            res = await _safe(interaction, client_rs.adjust(
                interaction.user.id, to_wallet, reason="bond redemption",
                idempotency_key=f"bond-{bond_id}-redeem"))
            if res is None:
                bdb.unclaim_bond(bond_id)
                return

        applied = _apply_to_overdue(interaction.user.id, garnish,
                                   f"garnished from bond #{bond_id}") if garnish else 0.0

        # Belt and braces: if less debt was there to settle than we withheld,
        # the remainder goes to savings rather than evaporating. Savings is a
        # local write that can't fail, so no coins are lost either way.
        shortfall = garnish - applied
        if shortfall > 0:
            bdb.add_savings(interaction.user.id, shortfall)
            bdb.log(interaction.user.id, "deposit", shortfall,
                    f"bond #{bond_id} garnish remainder -> savings")
            log.warning("Bond #%s garnish shortfall of %s credited to savings for %s",
                        bond_id, shortfall, interaction.user.id)

        bdb.finalize_bond_redemption(bond_id, amount, now.isoformat())
        bdb.log(interaction.user.id, "bond_redeem", amount, f"bond #{bond_id} {kind_note}")

    split = [("To your wallet", ab.coins(to_wallet))]
    if applied:
        split.append(("Taken for overdue debt", ab.coins(applied)))
        split.append(("Remaining debt", ab.coins(bdb.total_debt(interaction.user.id))))
    if shortfall > 0:
        split.append(("To your savings", ab.coins(shortfall)))
    if res:
        split.append(("Wallet", ab.coins(res["coins"])))

    await interaction.followup.send(
        # Green only where coins actually reached the reader.
        embed=ab.embed(title=f"Bond redeemed #{bond_id}",
                       kicker="/bond redeem",
                       desc=(f"{bond['term_days']} day term — {kind_note}."),
                       band=[("Payout", ab.coins(amount)),
                             ("Term", f"{bond['term_days']} days")],
                       groups=[("Where it went", ab.rows(split, strong="To your wallet"))],
                       colour=ab.GAIN if to_wallet > 0 else ab.ACCENT),
        ephemeral=True)

    asyncio.create_task(_log_activity(ab.line(
        "Bond redeemed", f"#{bond_id}", interaction.user.mention, ab.coins(amount),
        f"{bond['term_days']} day term", kind_note,
        f"{ab.coins(applied)} taken against overdue debt" if applied else "")))



invest_group = app_commands.Group(name="invest", description="Trade stocks on the Restocker exchange")



async def _market_autocomplete(interaction: discord.Interaction, current: str):
    if client_rs is None:
        return []
    try:
        markets = await client_rs.list_stocks()
    except RestockerError:
        return []
    cur = (current or "").lower()
    out = []
    for m in markets:
        label = f"{m['name']} ({m['market_id']}) — {ab.coins(m['price'], 2)} per share"
        if cur in m["market_id"].lower() or cur in m["name"].lower():
            out.append(app_commands.Choice(name=label[:100], value=m["market_id"]))
    return out[:25]




# ══════════════════════════════════════════════════════════════════════════
# Trading: quote, then confirm, then trade
# ══════════════════════════════════════════════════════════════════════════
#
# `/invest buy` used to execute on the first click, with no price shown and no
# bound on the fill. The price moves with each trade, so what a member paid was
# whatever the market happened to be when the request landed — they agreed to a
# share count and found out the cost afterwards.
#
# Now: quote the price, show what it costs, and execute only on a second,
# deliberate click, with the quote attached. Core refuses `slippage` before
# anything moves if the market has run past the bound, and that refusal releases
# the key so a fresh quote goes straight through.

#: How far the fill may drift from the quoted price before core refuses it.
#: 200 bps = 2%. A tighter bound refuses more often on a thin market; a looser
#: one is decoration.
INVEST_MAX_SLIPPAGE_BPS = int(os.getenv("INVEST_MAX_SLIPPAGE_BPS", "200"))


async def _quote_for(interaction: discord.Interaction, market_id: str):
    """The current price of one market, from the same list `/invest list` shows."""
    markets = await _safe(interaction, client_rs.list_stocks())
    if markets is None:
        return None
    for m in markets:
        if str(m.get("market_id")) == str(market_id):
            return m
    await interaction.followup.send(
        f"{market_id} is not a public market. Use /invest list to see what is.",
        ephemeral=True)
    return None


class _TradeConfirm(discord.ui.View):
    """Confirm a quoted trade. Only the member who asked can press it.

    The idempotency key is derived from the ORIGINAL command interaction, so it is
    the same key however many times Confirm is pressed: a double-click replays the
    first result instead of buying twice. A generated-per-call key would only have
    covered a transport retry, which is not the failure people actually hit.
    """

    def __init__(self, *, user_id: int, market_id: str, market_name: str,
                 shares: int, side: str, quote_price: float, total: int,
                 bound: int, key: str):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.market_id = market_id
        self.market_name = market_name
        self.shares = shares
        self.side = side
        self.quote_price = quote_price
        self.total = total
        self.bound = bound
        self.key = key
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This quote belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        # A quote that has gone stale must not sit there looking pressable.
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            await interaction.response.send_message("Already sent.", ephemeral=True)
            return
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        if self.side == "buy":
            res = await _safe(interaction, client_rs.stock_buy(
                self.user_id, self.market_id, self.shares,
                name=interaction.user.display_name,
                idempotency_key=self.key, quote_price=self.quote_price,
                max_total=self.bound, max_slippage_bps=INVEST_MAX_SLIPPAGE_BPS))
        else:
            res = await _safe(interaction, client_rs.stock_sell(
                self.user_id, self.market_id, self.shares,
                name=interaction.user.display_name,
                idempotency_key=self.key, quote_price=self.quote_price,
                min_total=self.bound, max_slippage_bps=INVEST_MAX_SLIPPAGE_BPS))
        if res is None:
            return

        if res.get("code") == "slippage":
            await interaction.followup.send(
                f"{self.market_name} moved past the price you agreed to, so nothing "
                f"was traded. Run the command again for a fresh quote.", ephemeral=True)
            return

        if res.get("ok"):
            kind = "stock_buy" if self.side == "buy" else "stock_sell"
            bdb.log(self.user_id, kind, self.shares, f"{self.market_id}")
            asyncio.create_task(_log_activity(ab.line(
                "Shares bought" if self.side == "buy" else "Shares sold",
                self.market_name, f"{self.shares:,} shares", interaction.user.mention)))
        await interaction.followup.send(res.get("message", "Done."), ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Nothing was traded.", ephemeral=True)


async def _quote_then_confirm(interaction: discord.Interaction, market: str,
                              shares: int, *, side: str) -> None:
    """Show the price and the total, then wait for a second click."""
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    quote = await _quote_for(interaction, market)
    if quote is None:
        return

    price = float(quote.get("price") or 0)
    name = quote.get("name") or market
    total = int(round(price * shares))
    slip = INVEST_MAX_SLIPPAGE_BPS / 10_000.0
    # The bound is on the coin figure the member agreed to, in their own
    # direction: a buyer is protected from paying more, a seller from receiving
    # less.
    bound = int(round(total * (1 + slip))) if side == "buy" else int(round(total * (1 - slip)))
    key = f"invest:{side}:{interaction.id}"

    view = _TradeConfirm(user_id=interaction.user.id, market_id=market,
                         market_name=name, shares=shares, side=side,
                         quote_price=price, total=total, bound=bound, key=key)
    verb = "pay" if side == "buy" else "receive"
    await interaction.followup.send(
        embed=ab.embed(
            title=f"{'Buy' if side == 'buy' else 'Sell'} {shares:,} "
                  f"share{'' if shares == 1 else 's'} of {name}",
            kicker=f"/invest {side}",
            desc=f"At {ab.coins(price, 2)} a share you {verb} about "
                 f"{ab.coins(total)}.",
            band=[("Shares", f"{shares:,}"),
                  ("Quoted price", ab.coins(price, 2)),
                  ("About", ab.coins(total))],
            groups=[("What happens next", ab.rows([
                ("Price moves with each trade",
                 f"refused if it drifts more than {INVEST_MAX_SLIPPAGE_BPS / 100:g}%"),
                ("You " + verb + " at most" if side == "buy"
                 else "You receive at least", ab.coins(bound)),
            ]))],
            foot="This quote expires in 60 seconds.",
            colour=ab.ACCENT),
        view=view, ephemeral=True)


@invest_group.command(name="list", description="List public markets you can invest in")

async def invest_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    markets = await _safe(interaction, client_rs.list_stocks())
    if markets is None:
        return
    if not markets:
        await interaction.followup.send("No public markets right now.", ephemeral=True)
        return
    rows = [(f"{m['name']} ({m['market_id']})",
             f"{ab.coins(m['price'], 2)} per share · P/E {ab.multiple(m['pe'], 1)}")
            for m in markets]
    await interaction.followup.send(
        embed=ab.embed(title="Public markets",
                       kicker="/invest list",
                       groups=[("Markets", ab.rows(rows))],
                       colour=ab.NEUTRAL),
        ephemeral=True)


@invest_group.command(name="buy", description="Buy shares (paid from your wallet)")
@app_commands.describe(market="The market to invest in", shares="How many shares")
@app_commands.autocomplete(market=_market_autocomplete)

async def invest_buy(interaction: discord.Interaction, market: str,
                     shares: app_commands.Range[int, 1, 1_000_000]):
    await _quote_then_confirm(interaction, market, int(shares), side="buy")


@invest_group.command(name="sell", description="Sell shares back to the market")
@app_commands.describe(market="The market you hold", shares="How many shares")
@app_commands.autocomplete(market=_market_autocomplete)

async def invest_sell(interaction: discord.Interaction, market: str,
                      shares: app_commands.Range[int, 1, 1_000_000]):
    await _quote_then_confirm(interaction, market, int(shares), side="sell")


@invest_group.command(name="portfolio", description="See your stock holdings")

async def invest_portfolio(interaction: discord.Interaction):
    if not await ensure_account(interaction, write=False):
        return
    await interaction.response.defer(ephemeral=True)
    holdings = await _safe(interaction, client_rs.portfolio(interaction.user.id))
    if holdings is None:
        return
    if not holdings:
        await interaction.followup.send("You don't hold any shares yet.", ephemeral=True)
        return
    rows, total = [], 0.0
    for h in holdings:
        total += h["value"]
        pl = h["value"] - h["cost_basis"]
        rows.append((h["market_id"],
                     f"{h['shares']:,.0f} shares at {ab.coins(h['price'], 2)} per share "
                     f"= {ab.coins(h['value'])} ({ab.signed(pl)})"))
    await interaction.followup.send(
        embed=ab.embed(title="Your portfolio",
                       kicker="/invest portfolio",
                       band=[("Total value", ab.coins(total)),
                             ("Markets held", str(len(holdings)))],
                       groups=[("Holdings", ab.rows(rows))],
                       colour=ab.ACCENT),
        ephemeral=True,
    )



admin_group = app_commands.Group(name="admin", description="Lead Banker tools")


@admin_group.command(name="account", description="Inspect any member's bank account")
@app_commands.describe(member="Whose account to look at")

async def admin_account(interaction: discord.Interaction, member: discord.Member):
    if not await ensure_banker(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    acct = bdb.get_account(member.id)
    if not acct:
        await interaction.followup.send(f"{member.mention} has never opened an account.", ephemeral=True)
        return

    sav = bdb.get_savings(member.id)["balance"]
    debt = bdb.total_debt(member.id)
    bonds = bdb.total_bonds_value(member.id)
    hist = bdb.loan_history(member.id)
    pending = bdb.get_pending_loans(member.id)
    overdue = [l for l in bdb.overdue_loans() if str(l["user_id"]) == str(member.id)]

    wallet = "Unknown"
    if client_rs is not None:
        try:
            wallet = ab.coins((await client_rs.get_balance(member.id))["coins"])
        except RestockerError as e:
            wallet = f"Unavailable: {e}"

    status = "Open" if acct["opted_in"] else "Closed"
    if acct.get("frozen"):
        status += f" · frozen ({acct.get('frozen_reason') or 'no reason given'})"

    opened = _parse_iso(str(acct["created_at"]))
    groups = [("Holdings", ab.rows([
                  ("Wallet", wallet),
                  ("Savings", ab.coins(sav)),
                  ("Bonds, value at maturity", ab.coins(bonds)),
              ])),
              ("Lending", ab.rows([
                  ("Debt", ab.coins(debt)),
                  ("Credit limit", ab.coins(credit_limit_for(member.id))
                                   + (" (override)" if acct.get("credit_limit") is not None else "")),
                  ("Overdue loans", len(overdue)),
                  ("Repaid", hist["repaid_count"]),
                  ("Late", hist["late_count"]),
                  ("Written off", hist["written_off_count"]),
                  ("Denied", hist["denied_count"]),
              ], strong="Debt"))]
    if pending:
        groups.append(("Awaiting approval", ab.rows(
            [(f"#{p['id']} · {p['term_days']} day term", ab.coins(p["principal"]))
             for p in pending])))

    e = ab.embed(title=member.display_name,
                 kicker="/admin account",
                 desc=f"`{member.id}` · {status}",
                 band=[("Savings", ab.coins(sav)),
                       ("Debt", ab.coins(debt)),
                       ("Bonds", ab.coins(bonds))],
                 groups=groups,
                 foot=f"Account opened {ab.on_day(opened)}" if opened else "",
                 colour=ab.LOSS if overdue else ab.ACCENT)
    await interaction.followup.send(embed=e, ephemeral=True)


@admin_group.command(name="freeze", description="Freeze an account so it can't move money")
@app_commands.describe(member="Whose account", reason="Shown to them when they try a command")

async def admin_freeze(interaction: discord.Interaction, member: discord.Member, reason: str = ""):
    if not await ensure_banker(interaction):
        return
    if not bdb.get_account(member.id):
        await interaction.response.send_message(
            f"{member.mention} has no bank account.", ephemeral=True)
        return
    bdb.set_frozen(member.id, True, reason)
    bdb.log(member.id, "account_frozen", 0, f"by {interaction.user.id}: {reason}")
    await interaction.response.send_message(
        f"Froze {member.mention}'s account.", ephemeral=True)
    asyncio.create_task(_log_activity(ab.line(
        "Account frozen", member.mention, f"by {interaction.user.mention}",
        f"reason: {reason}" if reason else "")))


@admin_group.command(name="unfreeze", description="Lift a freeze")
@app_commands.describe(member="Whose account")

async def admin_unfreeze(interaction: discord.Interaction, member: discord.Member):
    if not await ensure_banker(interaction):
        return
    bdb.set_frozen(member.id, False)
    bdb.log(member.id, "account_unfrozen", 0, f"by {interaction.user.id}")
    await interaction.response.send_message(
        f"Unfroze {member.mention}'s account.", ephemeral=True)
    asyncio.create_task(_log_activity(ab.line(
        "Account unfrozen", member.mention, f"by {interaction.user.mention}")))


@admin_group.command(name="creditlimit", description="Override how much a member may borrow")
@app_commands.describe(member="Whose limit", limit="New limit, or -1 to clear the override")

async def admin_creditlimit(interaction: discord.Interaction, member: discord.Member,
                            limit: app_commands.Range[int, -1, 100_000_000]):
    if not await ensure_banker(interaction):
        return
    if limit < 0:
        bdb.set_credit_limit(member.id, None)
        msg = (f"Cleared {member.mention}'s override — back to the earned limit of "
               f"{ab.coins(credit_limit_for(member.id))}.")
    else:
        bdb.set_credit_limit(member.id, limit)
        msg = f"Set {member.mention}'s credit limit to {ab.coins(limit)}."
    bdb.log(member.id, "credit_limit_set", max(0, limit), f"by {interaction.user.id}")
    await interaction.response.send_message(msg, ephemeral=True)
    asyncio.create_task(_log_activity(ab.line(
        "Credit limit", f"set by {interaction.user.mention}", msg)))


@admin_group.command(name="savings", description="Adjust a member's savings (corrections, fines, payouts)")
@app_commands.describe(member="Whose savings", amount="Positive to credit, negative to debit",
                       reason="Why — goes in the ledger")

async def admin_savings(interaction: discord.Interaction, member: discord.Member,
                        amount: app_commands.Range[int, -100_000_000, 100_000_000],
                        reason: str):
    if not await ensure_banker(interaction):
        return
    if amount == 0:
        await interaction.response.send_message("Amount can't be zero.", ephemeral=True)
        return
    if not bdb.get_account(member.id):
        await interaction.response.send_message(
            f"{member.mention} has no bank account.", ephemeral=True)
        return
    if amount < 0 and not bdb.try_debit_savings(member.id, -amount):
        cur = bdb.get_savings(member.id)["balance"]
        await interaction.response.send_message(
            f"They only have {ab.coins(cur)} in savings.", ephemeral=True)
        return
    if amount > 0:
        bdb.add_savings(member.id, amount)
    new = bdb.get_savings(member.id)["balance"]
    bdb.log(member.id, "admin_savings_adjust", amount, f"by {interaction.user.id}: {reason}")
    await interaction.response.send_message(
        f"{'Credited' if amount > 0 else 'Debited'} {ab.coins(abs(amount))} "
        f"{'to' if amount > 0 else 'from'} {member.mention}'s savings. "
        f"New balance: {ab.coins(new)}.", ephemeral=True)
    asyncio.create_task(_log_activity(ab.line(
        "Savings adjusted", member.mention, ab.signed(amount),
        f"by {interaction.user.mention}", reason)))


@admin_group.command(name="forgive", description="Write off a loan (the debt disappears, no coins move)")
@app_commands.describe(member="Whose loan", loan_id="Loan number, or omit to forgive all their debt")

async def admin_forgive(interaction: discord.Interaction, member: discord.Member,
                        loan_id: int | None = None):
    if not await ensure_banker(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    async with _user_lock(member.id):
        if loan_id is None:
            targets = bdb.get_active_loans(member.id)
        else:
            l = bdb.get_loan(loan_id)
            if not l or str(l["user_id"]) != str(member.id):
                await interaction.followup.send(
                    f"Loan #{loan_id} isn't {member.display_name}'s.", ephemeral=True)
                return
            targets = [l]
        if not targets:
            await interaction.followup.send(
                f"{member.mention} has no active loans.", ephemeral=True)
            return
        wiped, ids = 0.0, []
        for l in targets:
            done = bdb.write_off_loan(l["id"], interaction.user.id)
            if done:
                wiped += float(l["balance"])
                ids.append(l["id"])
                bdb.log(member.id, "loan_written_off", float(l["balance"]),
                        f"loan #{l['id']} by {interaction.user.id}")
    if not ids:
        await interaction.followup.send("Nothing to forgive — those loans aren't active.", ephemeral=True)
        return
    await interaction.followup.send(
        embed=ab.embed(title="Loans written off",
                       kicker="/admin forgive",
                       desc=(f"Wrote off {ab.coins(wiped)} for {member.mention} "
                             f"(loan{'s' if len(ids) > 1 else ''} "
                             f"{', '.join('#' + str(i) for i in ids)})."),
                       foot=("A written-off loan drops their earned credit limit to 0 "
                             "until an override is set with /admin creditlimit."),
                       colour=ab.LOSS),
        ephemeral=True)
    asyncio.create_task(_log_activity(ab.line(
        "Debt written off", member.mention, ab.coins(wiped),
        f"by {interaction.user.mention}")))


@admin_group.command(name="loans", description="Loan requests waiting for a decision")


async def admin_loans(interaction: discord.Interaction):
    if not await ensure_banker(interaction):
        return
    pending = bdb.get_pending_loans()
    if not pending:
        await interaction.response.send_message("No loan requests pending.", ephemeral=True)
        return
    rows = []
    for p in pending[:25]:
        asked = _parse_iso(str(p["requested_at"]))
        rows.append((f"#{p['id']} · <@{p['user_id']}> · {p['term_days']} day term",
                     ab.line(ab.coins(p["principal"]),
                             f"asked {ab.when(asked)}" if asked else "")))
    foot = (f"Showing 25 of {len(pending)}. " if len(pending) > 25 else "")
    await interaction.response.send_message(
        embed=ab.embed(title="Pending loan requests",
                       kicker="/admin loans",
                       groups=[("Requests", ab.rows(rows))],
                       foot=foot + "Decide with /admin approve, /admin deny, or the "
                                   "buttons on the proposal message.",
                       colour=ab.NEUTRAL),
        ephemeral=True)


@admin_group.command(name="approve", description="Approve a pending loan by number")
@app_commands.describe(loan_id="Loan number from /admin loans")
async def admin_approve(interaction: discord.Interaction, loan_id: int):
    if not await ensure_banker(interaction):
        return
    await approve_loan(interaction, loan_id)


@admin_group.command(name="deny", description="Deny a pending loan by number")
@app_commands.describe(loan_id="Loan number from /admin loans")
async def admin_deny(interaction: discord.Interaction, loan_id: int):
    if not await ensure_banker(interaction):
        return
    await deny_loan(interaction, loan_id)


@admin_group.command(name="overdue", description="Everyone currently in default")

async def admin_overdue(interaction: discord.Interaction):
    if not await ensure_banker(interaction):
        return
    loans = bdb.overdue_loans()
    if not loans:
        await interaction.response.send_message("Nobody is overdue.", ephemeral=True)
        return
    rows, total = [], 0.0
    for l in loans[:25]:
        due = _parse_iso(l["due_at"])
        total += float(l["balance"])
        sav = bdb.get_savings(l["user_id"])["balance"]
        term = f"{l['term_days']} day term" if l.get("term_days") else "term not recorded"
        rows.append((f"#{l['id']} · <@{l['user_id']}> · {term}",
                     f"{ab.coins(l['balance'])} owed · due {ab.when(due) if due else 'unknown'} "
                     f"· savings {ab.coins(sav)}"))
    foot = (f"Showing 25 of {len(loans)}. " if len(loans) > 25 else "")
    foot += (f"Savings are collected automatically {COLLECT_GRACE_DAYS:g} days past due; "
             f"wallets are never touched."
             if COLLECT_FROM_SAVINGS else
             "Automatic collection is off (COLLECT_FROM_SAVINGS=0).")
    await interaction.response.send_message(
        embed=ab.embed(title="Overdue loans",
                       kicker="/admin overdue",
                       band=[("Total overdue", ab.coins(total)),
                             ("Loans", str(len(loans)))],
                       groups=[("Loans", ab.rows(rows))],
                       foot=foot,
                       colour=ab.LOSS),
        ephemeral=True)


@admin_group.command(name="collect", description="Run the collections pass right now")

async def admin_collect(interaction: discord.Interaction):
    if not await ensure_banker(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    before = sum(float(l["balance"]) for l in bdb.overdue_loans())
    try:
        await run_collections()
    except Exception as e:
        log.exception("Manual collections pass failed")
        await interaction.followup.send(f"Collections failed: {e}", ephemeral=True)
        return
    after = sum(float(l["balance"]) for l in bdb.overdue_loans())
    await interaction.followup.send(
        embed=ab.embed(title="Collections run",
                       kicker="/admin collect",
                       band=[("Before", ab.coins(before)),
                             ("After", ab.coins(after)),
                             ("Recovered", ab.coins(max(0.0, before - after)))],
                       foot="Taken from savings only, never from wallets.",
                       colour=ab.ACCENT),
        ephemeral=True)


@admin_group.command(name="close", description="Force-close a member's account")
@app_commands.describe(member="Whose account", reason="Why")

async def admin_close(interaction: discord.Interaction, member: discord.Member, reason: str = ""):
    if not await ensure_banker(interaction):
        return
    if not bdb.get_account(member.id):
        await interaction.response.send_message(
            f"{member.mention} has no bank account.", ephemeral=True)
        return
    debt = bdb.total_debt(member.id)
    if debt > 0:
        await interaction.response.send_message(
            f"{member.mention} still owes {ab.coins(debt)}. Collect or `/admin forgive` "
            f"it first.", ephemeral=True)
        return
    bdb.close_account(member.id)
    bdb.log(member.id, "account_closed", 0, f"forced by {interaction.user.id}: {reason}")
    sav = bdb.get_savings(member.id)["balance"]
    note = (f" They still have {ab.coins(sav)} in savings and "
            f"{len(bdb.get_bonds(member.id, 'active'))} active bond(s) — reopening the "
            f"account with `/bank open` restores access to them." if sav > 0 else "")
    await interaction.response.send_message(
        f"Closed {member.mention}'s account.{note}", ephemeral=True)
    asyncio.create_task(_log_activity(ab.line(
        "Account force-closed", member.mention, f"by {interaction.user.mention}",
        f"reason: {reason}" if reason else "")))


@admin_group.command(name="stats", description="Bank-wide totals")

async def admin_stats(interaction: discord.Interaction):
    if not await ensure_banker(interaction):
        return
    s = bdb.bank_stats()
    overdue = bdb.overdue_loans()
    # What the bank owes depositors vs what it's owed back.
    liabilities = float(s["savings_total"]) + float(s["bonds_payout"])
    assets = float(s["debt_total"])
    e = ab.embed(
        title="Bank of Osentar books",
        kicker="/admin stats",
        band=[("Savings held", ab.coins(s["savings_total"])),
              ("Loans out", ab.coins(s["debt_total"])),
              ("Overdue", ab.coins(sum(float(l["balance"]) for l in overdue)))],
        groups=[("Accounts", ab.rows([
                    ("Open", s["accounts_open"]),
                    ("Closed", s["accounts_closed"]),
                    ("Frozen", s["accounts_frozen"]),
                ])),
                ("Bonds", ab.rows([
                    ("Principal locked", ab.coins(s["bonds_locked"])),
                    ("Payout liability", ab.coins(s["bonds_payout"])),
                ])),
                ("Lending", ab.rows([
                    ("Active loans", s["loans_active"]),
                    ("Pending requests", s["loans_pending"]),
                    ("Overdue loans", len(overdue)),
                    ("Written off", ab.coins(s["written_off_total"])),
                ])),
                ("Position", ab.rows([
                    ("Owed to members", ab.coins(liabilities)),
                    ("Owed to the bank", ab.coins(assets)),
                    ("Net", ab.coins(assets - liabilities)),
                ], strong="Net"))],
        colour=ab.ACCENT)
    await interaction.response.send_message(embed=e, ephemeral=True)


@admin_group.command(name="config", description="Show the bank's effective settings")

async def admin_config(interaction: discord.Interaction):
    if not await ensure_banker(interaction):
        return
    terms = ", ".join(f"{d} day at {a*100:.1f}% APR" for d, a in BOND_TERMS.items()) or "none"
    staff = (f"{len(LEAD_BANKER_ROLE_IDS)} role(s), {len(BANK_ADMIN_USER_IDS)} user(s) allow-listed"
             if (LEAD_BANKER_ROLE_IDS or BANK_ADMIN_USER_IDS)
             else "Neither set — falling back to server Administrators")
    e = ab.embed(
        title="Effective config",
        kicker="/admin config",
        groups=[("Rates", ab.rows([
                    ("Savings APR", f"{SAVINGS_APR*100:.2f}%"),
                    ("Loan APR", f"{LOAN_APR*100:.2f}%"),
                    ("Overdue surcharge", f"+{LOAN_OVERDUE_EXTRA_APR*100:.0f}% APR"),
                ])),
                ("Lending", ab.rows([
                    ("Approval gate", "on" if LOAN_REQUIRE_APPROVAL else "off"),
                    ("Base credit limit", ab.coins(BASE_CREDIT_LIMIT)),
                    ("Per repaid loan", ab.signed(CREDIT_PER_REPAID_LOAN)),
                    ("Per late loan", ab.signed(-CREDIT_LATE_PENALTY)),
                    ("Hard cap", ab.coins(MAX_LOAN)),
                    ("Default term", f"{DEFAULT_LOAN_DAYS} days"),
                ])),
                ("Collections", ab.rows([
                    ("Take from savings", "on" if COLLECT_FROM_SAVINGS else "off"),
                    ("Grace before savings are taken", f"{COLLECT_GRACE_DAYS:g} days"),
                    ("Take from bond payouts", "on" if GARNISH_BOND_PAYOUTS else "off"),
                    ("Announce overdue", "on" if OVERDUE_ANNOUNCE else "off"),
                    ("Wallets", "never touched"),
                ])),
                ("Bonds", ab.rows([
                    ("Terms", terms),
                    ("Early redemption penalty", f"{BOND_EARLY_PENALTY_PCT*100:.0f}%"),
                ])),
                ("Access", ab.rows([
                    ("Staff", staff),
                    ("Restocker", RESTOCKER_API_URL or "not configured"),
                ]))],
        colour=ab.NEUTRAL)
    await interaction.response.send_message(embed=e, ephemeral=True)


def _overdue_debt(user_id) -> float:
    """Balance across this user's loans that are past due right now."""
    return sum(float(l["balance"]) for l in bdb.overdue_loans() if str(l["user_id"]) == str(user_id))


def _apply_to_overdue(user_id, amount: float, meta: str) -> float:
    """Pay `amount` against the user's overdue loans, oldest due first.
    Returns how much was actually applied. Purely local — no coins move in
    Restocker, because the money is already inside the bank."""
    remaining = float(amount)
    applied = 0.0
    for l in bdb.overdue_loans():
        if remaining <= 0:
            break
        if str(l["user_id"]) != str(user_id):
            continue
        chunk = min(remaining, float(l["balance"]))
        if chunk <= 0:
            continue
        bdb.apply_loan_payment(l["id"], chunk)
        bdb.record_collection(l["id"], chunk)
        bdb.log(user_id, "loan_collect", chunk, f"loan #{l['id']} {meta}")
        remaining -= chunk
        applied += chunk
    return applied



async def run_collections():
    """Chase overdue loans.

    Two things happen here, both idempotent so the hourly loop can run forever:
      1. The first time a loan goes past due it gets announced once (the
         overdue_notified flag makes it once, not once per hour).
      2. After COLLECT_GRACE_DAYS, savings are seized against the debt. Savings
         sit inside the bank already, so this is a local transfer — no Restocker
         call, nothing that can half-fail.
    The wallet is never touched: the bank can take what it holds, not reach into
    someone's pocket.
    """
    if not (COLLECT_FROM_SAVINGS or OVERDUE_ANNOUNCE):
        return
    now = utcnow()
    grace_cutoff = (now - timedelta(days=COLLECT_GRACE_DAYS)).isoformat()

    for loan in bdb.overdue_loans(now.isoformat()):
        uid = loan["user_id"]
        loan_id = loan["id"]

        if OVERDUE_ANNOUNCE and bdb.mark_overdue_notified(loan_id):
            due_dt = _parse_iso(loan["due_at"])
            await _log_activity(ab.line(
                "Overdue", f"loan #{loan_id}", f"<@{uid}>",
                f"{ab.coins(loan['balance'])} owed",
                f"{loan['term_days']} day term" if loan.get("term_days") else "",
                f"was due {ab.when(due_dt)}" if due_dt else "",
                "penalty APR now applies"))

        if not COLLECT_FROM_SAVINGS:
            continue
        if (loan["due_at"] or "") > grace_cutoff:
            continue  # still inside the grace period

        async with _user_lock(uid):
            fresh = bdb.get_loan(loan_id)
            if not fresh or fresh["status"] != "active" or float(fresh["balance"]) <= 0:
                continue
            owed = float(fresh["balance"])
            savings = float(bdb.get_savings(uid)["balance"])
            take = math.floor(min(owed, savings))
            if take < 1:
                continue
            if not bdb.try_debit_savings(uid, take):
                continue  # lost a race with a withdrawal; next pass will retry
            bdb.apply_loan_payment(loan_id, take)
            bdb.record_collection(loan_id, take)
            bdb.log(uid, "loan_collect", take, f"loan #{loan_id} seized from savings")
            left = bdb.total_debt(uid)

        log.info("[collections] seized %s from savings of %s for loan #%s", take, uid, loan_id)
        await _log_activity(ab.line(
            "Collected", f"loan #{loan_id}", f"<@{uid}>", ab.coins(take), "from savings",
            f"remaining debt {ab.coins(left)}"))


@tasks.loop(hours=1)
async def accrue_interest():
    """Compound savings (credit) and loans (debit) based on REAL elapsed time
    since each row was last accrued. Because it advances last_accrued to 'now'
    on each applied pass, it is correct across restarts, redeploys and downtime —
    no double-counting on restart, no skipped days after an outage. Loans past
    their due_at accrue at a penalty rate for the overdue portion of the period."""
    now = utcnow()
    daily_sav = SAVINGS_APR / 365.0
    daily_loan = LOAN_APR / 365.0
    daily_loan_overdue = (LOAN_APR + LOAN_OVERDUE_EXTRA_APR) / 365.0

    for s in bdb.all_savings():
        bal = float(s["balance"])
        last = _parse_iso(s.get("last_accrued"))
        if last is None:
            bdb.set_savings_accrued(s["user_id"], now.isoformat(), bal)
            continue
        days = (now - last).total_seconds() / 86400.0
        if days <= 0 or bal <= 0:
            continue
        new_bal = bal * ((1.0 + daily_sav) ** days)
        interest = new_bal - bal
        if interest < _MIN_ACCRUAL:
            continue
        bdb.set_savings_accrued(s["user_id"], now.isoformat(), new_bal)
        bdb.log(s["user_id"], "interest_savings", interest, f"{days:.3f}d")

    for l in bdb.all_active_loans():
        bal = float(l["balance"])
        last = _parse_iso(l.get("last_accrued"))
        if last is None:
            bdb.set_loan_accrued(l["id"], now.isoformat(), bal)
            continue
        if bal <= 0:
            continue
        total_days = (now - last).total_seconds() / 86400.0
        if total_days <= 0:
            continue
        due = _parse_iso(l.get("due_at"))
        if due is None or now <= due:
            on_time_days, overdue_days = total_days, 0.0
        elif last >= due:
            on_time_days, overdue_days = 0.0, total_days
        else:
            on_time_days = (due - last).total_seconds() / 86400.0
            overdue_days = (now - due).total_seconds() / 86400.0
        factor = ((1.0 + daily_loan) ** on_time_days) * ((1.0 + daily_loan_overdue) ** overdue_days)
        new_bal = bal * factor
        interest = new_bal - bal
        if interest < _MIN_ACCRUAL:
            continue
        bdb.set_loan_accrued(l["id"], now.isoformat(), new_bal)
        meta = f"{total_days:.3f}d" + (f" (+{overdue_days:.2f}d overdue)" if overdue_days > 0 else "")
        bdb.log(l["user_id"], "interest_loan", interest, f"loan #{l['id']} {meta}")

    log.debug("[interest] elapsed-time accrual pass complete")

    # Collections run right after accrual so they act on today's real balances.
    # Isolated: a failure here must not stop next hour's interest.
    try:
        await run_collections()
    except Exception:
        log.exception("[collections] pass failed")


@accrue_interest.before_loop
async def _before_accrue():
    await bot.wait_until_ready()



@bot.event
async def on_ready():
    bdb.init_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        log.info("Slash commands synced.")
    except Exception as e:
        log.exception("Command sync failed: %s", e)

    if RESTOCKER_API_URL and not RESTOCKER_API_URL.lower().startswith("https://"):
        log.warning("SECURITY: RESTOCKER_API_URL is not HTTPS — the bank token would "
                    "travel in plaintext. Use an https:// URL in production.")

    if client_rs is not None:
        try:
            h = await client_rs.health()
            if not h.get("enabled"):
                log.warning("Restocker bank API is reachable but DISABLED "
                            "(BANK_API_TOKEN not set on the Restocker server).")
            ok_ver, server_ver = await client_rs.check_version()
            if not ok_ver:
                log.warning("Bank API version mismatch: server=%s, expected=%s. "
                            "Update both bots to the same version.", server_ver, EXPECTED_API_VERSION)
            await client_rs.ping()
            log.info("Connected to Restocker bank API v%s at %s", server_ver, RESTOCKER_API_URL)
        except RestockerError as e:
            log.warning("Could not reach Restocker bank API: %s", e)
    else:
        log.warning("Restocker API not configured — wallet/stock commands will be disabled.")

    if not (LEAD_BANKER_ROLE_IDS or BANK_ADMIN_USER_IDS):
        log.warning("No LEAD_BANKER_ROLE_IDS or BANK_ADMIN_USER_IDS set — /admin is "
                    "restricted to server Administrators only.")
    if LOAN_REQUIRE_APPROVAL and not LOAN_PROPOSALS_CHANNEL_ID:
        log.warning("LOAN_REQUIRE_APPROVAL is on but LOAN_PROPOSALS_CHANNEL_ID is empty — "
                    "requests can only be found with /admin loans.")

    pending = len(bdb.get_pending_loans())
    if pending:
        log.info("%d loan request(s) awaiting approval.", pending)

    if not accrue_interest.is_running():
        accrue_interest.start()
    log.info("Bank bot ready as %s", bot.user)


async def _on_close():
    if client_rs is not None:
        await client_rs.close()


def main():
    if not DISCORD_TOKEN:
        raise SystemExit("BANK_DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bdb.init_db()
    for grp in (bank_group, loan_group, savings_group, bond_group, invest_group, admin_group):
        bot.tree.add_command(grp)
    # Teaches the bot how to rebuild Approve/Deny buttons from a click, so loan
    # proposals posted before a restart stay live afterwards.
    bot.add_dynamic_items(LoanDecisionButton)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

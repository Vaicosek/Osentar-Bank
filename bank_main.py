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
            "🧊 Your account is frozen, so you can't move money right now."
            + (f"\nReason: {reason}" if reason else "")
            + "\nContact a Lead Banker.",
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
    await _reply(interaction, "⛔ That command is for Lead Bankers only.", error=True)
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


def _embed(title: str, desc: str = "", color: int = 0x2ECC71) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


async def _safe(interaction: discord.Interaction, coro):
    """Run a client coroutine, surfacing RestockerError as an ephemeral message.
    Returns the result, or None if it failed (message already sent)."""
    if client_rs is None:
        await _reply(interaction, "⚠️ The bank isn't connected to Restocker (missing config).", error=True)
        return None
    try:
        return await coro
    except RestockerError as e:
        if e.code == "insufficient":
            await _reply(interaction, "❌ Not enough coins in your wallet for that.", error=True)
        else:
            await _reply(interaction, f"❌ Restocker error: {e}", error=True)
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
    Bankers to review, with ✅/❌ reactions to mark it approved/denied.

    Best-effort and fire-and-forget: this never raises into the caller, so a
    missing channel/permission can't break /bank open for the user opening
    the account. Failures are only logged.
    """
    if not NEW_ACCOUNT_CHANNEL_ID:
        return
    try:
        channel = await _get_channel(NEW_ACCOUNT_CHANNEL_ID)
        embed = _embed(
            "🎫 New account — pending review",
            f"{member.mention} (`{member.id}`) opened a bank account.\n"
            f"Opened: {utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            color=0x3498DB,
        )
        msg = await channel.send(embed=embed)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except Exception:
        log.exception("Failed to post new-account ticket (channel %s) for user %s",
                      NEW_ACCOUNT_CHANNEL_ID, member.id)


def _loan_proposal_embed(member: discord.abc.User, loan: dict, days: int,
                         history: dict, limit: int, existing_debt: float) -> discord.Embed:
    """The card Lead Bankers actually decide from — the request plus the
    borrower's track record, so the decision doesn't need a second lookup."""
    e = _embed(
        "📜 Loan request — awaiting approval",
        f"{member.mention} (`{member.id}`) wants **{fmt(loan['principal'])}** {COIN} "
        f"for **{days} days** at {LOAN_APR*100:.1f}% APR.\nLoan #{loan['id']}",
        color=0xE67E22,
    )
    e.add_field(name="Existing debt", value=f"{fmt(existing_debt)} {COIN}", inline=True)
    e.add_field(name="Credit limit", value=f"{fmt(limit)} {COIN}", inline=True)
    e.add_field(name="Loans repaid", value=str(history["repaid_count"]), inline=True)
    e.add_field(name="Times late", value=str(history["late_count"]), inline=True)
    e.add_field(name="Written off", value=str(history["written_off_count"]), inline=True)
    e.add_field(name="Previously denied", value=str(history["denied_count"]), inline=True)
    e.set_footer(text="No coins have moved yet. Approve to disburse.")
    return e


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
            embed = _embed(
                "📜 Loan issued",
                f"{member.mention} (`{member.id}`) borrowed **{fmt(loan['principal'])}** {COIN} "
                f"(loan #{loan['id']}).\nAPR {LOAN_APR*100:.1f}% · due in {days} days.",
                color=0xE67E22,
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
        super().__init__(
            discord.ui.Button(
                label="Approve" if action == "approve" else "Deny",
                emoji="✅" if action == "approve" else "❌",
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
    the channel shows what was decided and nobody can click it twice."""
    msg = getattr(interaction, "message", None)
    if msg is None:
        return
    try:
        embed = msg.embeds[0] if msg.embeds else _embed(title)
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
        await interaction.followup.send("❌ No such loan.", ephemeral=True)
        return
    if loan["status"] != "pending":
        await interaction.followup.send(
            f"❌ Loan #{loan_id} is already **{loan['status']}** — nothing to approve.",
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
                "❌ Someone else just decided that loan.", ephemeral=True)
            return

        acct = bdb.get_account(borrower_id) or {}
        if not acct.get("opted_in"):
            await interaction.followup.send(
                f"❌ Loan #{loan_id}: the borrower's account is closed.", ephemeral=True)
            return
        if acct.get("frozen"):
            await interaction.followup.send(
                f"❌ Loan #{loan_id}: the borrower's account is frozen. Unfreeze it first.",
                ephemeral=True)
            return

        # Re-check the limit at approval time — debt can have grown since the request.
        principal = float(loan["principal"])
        limit = credit_limit_for(borrower_id)
        debt = bdb.total_debt(borrower_id)
        if debt + principal > limit:
            await interaction.followup.send(
                f"⚠️ Loan #{loan_id} would put them at {fmt(debt + principal)} {COIN} against a "
                f"{fmt(limit)} {COIN} limit. Raise it with `/admin creditlimit` or deny.",
                ephemeral=True)
            return

        if not bdb.claim_pending_loan(loan_id, interaction.user.id):
            await interaction.followup.send("❌ Someone else just decided that loan.", ephemeral=True)
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

    await interaction.followup.send(
        f"✅ Approved loan #{loan_id} — **{fmt(principal)}** {COIN} disbursed.", ephemeral=True)
    await _stamp_proposal(
        interaction, "✅ Loan approved",
        f"Approved by {interaction.user.display_name} · due {due[:10]}", 0x2ECC71)
    asyncio.create_task(_log_activity(
        f"✅ Loan #{loan_id} — **{fmt(principal)}** {COIN} to <@{borrower_id}>, "
        f"approved by {interaction.user.mention}. Due in {days}d."))


async def deny_loan(interaction: discord.Interaction, loan_id: int) -> None:
    await interaction.response.defer(ephemeral=True)
    loan = bdb.get_loan(loan_id)
    if not loan:
        await interaction.followup.send("❌ No such loan.", ephemeral=True)
        return
    if loan["status"] != "pending":
        await interaction.followup.send(
            f"❌ Loan #{loan_id} is already **{loan['status']}**.", ephemeral=True)
        return
    if not bdb.deny_loan(loan_id, interaction.user.id):
        await interaction.followup.send("❌ Someone else just decided that loan.", ephemeral=True)
        return
    await interaction.followup.send(f"❌ Denied loan #{loan_id}.", ephemeral=True)
    await _stamp_proposal(interaction, "❌ Loan denied",
                          f"Denied by {interaction.user.display_name}", 0xE74C3C)
    asyncio.create_task(_log_activity(
        f"❌ Loan #{loan_id} for <@{loan['user_id']}> denied by {interaction.user.mention}."))


async def _log_activity(text: str) -> None:
    """Post one audit-trail line to BOT_LOG_CHANNEL_ID. Best-effort/fire-and-
    forget — failures are only logged, never surfaced to the user."""
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
        desc = (f"Your bank account is open, **{interaction.user.display_name}**.\n"
                f"Try `/bank deposit`, `/loan request`, or `/invest list`.")
    else:
        desc = f"Welcome back — your account is active, **{interaction.user.display_name}**."
    await interaction.response.send_message(
        embed=_embed("🏦 Account ready", desc),
        ephemeral=True,
    )
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
    e = _embed(f"🏦 {interaction.user.display_name}'s bank")
    e.add_field(name="Wallet", value=f"{fmt(wallet['coins'])} {COIN}", inline=True)
    e.add_field(name="Savings", value=f"{fmt(sav)} {COIN}", inline=True)
    e.add_field(name="Bonds (at maturity)", value=f"{fmt(bonds)} {COIN}", inline=True)
    e.add_field(name="Debt", value=f"{fmt(debt)} {COIN}", inline=True)
    e.add_field(name="Net worth", value=f"**{fmt(net)}** {COIN}", inline=False)
    e.set_footer(text=f"Savings APR {SAVINGS_APR*100:.1f}% · Loan APR {LOAN_APR*100:.1f}%")
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
        embed=_embed("💰 Deposit complete",
                     f"Moved **{fmt(amount)}** {COIN} into savings.\n"
                     f"Savings balance: **{fmt(new_sav)}** {COIN}\n"
                     f"Wallet: {fmt(res['coins'])} {COIN}"),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(
        f"💰 {interaction.user.mention} deposited **{fmt(amount)}** {COIN} into savings."))


@bank_group.command(name="withdraw", description="Move coins from savings back to your wallet")
@app_commands.describe(amount="How many coins to withdraw")
async def bank_withdraw(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100_000_000]):
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    if not bdb.try_debit_savings(interaction.user.id, amount):
        sav = bdb.get_savings(interaction.user.id)["balance"]
        await interaction.followup.send(
            f"❌ You only have {fmt(sav)} {COIN} in savings.", ephemeral=True)
        return
    res = await _safe(interaction, client_rs.adjust(interaction.user.id, amount, reason="bank withdraw"))
    if res is None:
        bdb.add_savings(interaction.user.id, amount)
        return
    new_sav = bdb.get_savings(interaction.user.id)["balance"]
    bdb.log(interaction.user.id, "withdraw", amount, "savings->wallet")
    await interaction.followup.send(
        embed=_embed("🏧 Withdrawal complete",
                     f"Moved **{fmt(amount)}** {COIN} to your wallet.\n"
                     f"Savings balance: **{fmt(new_sav)}** {COIN}\n"
                     f"Wallet: {fmt(res['coins'])} {COIN}"),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(
        f"🏧 {interaction.user.mention} withdrew **{fmt(amount)}** {COIN} from savings."))


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
        embed=_embed("📤 Payment sent",
                     f"Sent **{fmt(amount)}** {COIN} to {member.mention}.\n"
                     f"Your wallet: {fmt(res['from']['coins'])} {COIN}"),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(
        f"📤 {interaction.user.mention} sent **{fmt(amount)}** {COIN} to {member.mention}."))


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
            lines.append(f"`{ts}`  {r['kind']}")
            continue
        if r["kind"] == "admin_savings_adjust":
            sign = "+" if r["amount"] >= 0 else "-"   # the amount itself carries the direction
        else:
            sign = "+" if r["kind"] in GAINS else "-"
        lines.append(f"`{ts}`  {r['kind']:<20} {sign}{fmt(abs(r['amount']))} {COIN}")
    await interaction.response.send_message(
        embed=_embed("📒 Recent activity", "\n".join(lines)), ephemeral=True)


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
            f"❌ You still owe **{fmt(debt)}** {COIN}. Repay with `/loan repay` before closing your account.",
            ephemeral=True)
        return
    active_bonds = bdb.get_bonds(interaction.user.id, "active")
    if active_bonds:
        await interaction.response.send_message(
            f"❌ You have {len(active_bonds)} active bond(s). Redeem them with `/bond redeem` "
            f"before closing your account.",
            ephemeral=True)
        return

    sav = bdb.get_savings(interaction.user.id)["balance"]
    cashout_note = f"This will move **{fmt(sav)}** {COIN} from savings to your wallet and " if sav > 0 else "This will "
    view = _CloseAccountConfirm(interaction.user.id)
    await interaction.response.send_message(
        embed=_embed("⚠️ Close your bank account?",
                     f"{cashout_note}deactivate your bank account.\n"
                     f"Your history isn't deleted — `/bank open` reopens it any time.",
                     color=0xE74C3C),
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
        embed=_embed("🔒 Account closed",
                     "Your bank account is now closed."
                     + (f" **{fmt(sav)}** {COIN} was moved to your wallet." if sav > 0 else "")),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(
        f"🔒 {interaction.user.mention} closed their bank account."
        + (f" Cashed out **{fmt(sav)}** {COIN} from savings." if sav > 0 else "")))



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
            "❌ Your credit limit is **0** — the bank isn't lending to you right now. "
            "Talk to a Lead Banker.", ephemeral=True)
        return
    if current_debt + amount > limit:
        await interaction.followup.send(
            f"❌ That would put your debt at {fmt(current_debt + amount)} {COIN}, over your "
            f"**{fmt(limit)}** {COIN} credit limit (current debt {fmt(current_debt)} {COIN}).\n"
            f"Your limit grows as you repay loans on time.", ephemeral=True)
        return

    pending = bdb.get_pending_loans(interaction.user.id)
    if LOAN_REQUIRE_APPROVAL and len(pending) >= MAX_PENDING_LOANS:
        await interaction.followup.send(
            f"❌ You already have {len(pending)} loan request awaiting approval "
            f"(#{pending[0]['id']}). Wait for a decision first.", ephemeral=True)
        return

    if LOAN_REQUIRE_APPROVAL:
        # No coins move here. The loan sits at status='pending' — not counted as
        # debt, no interest — until a Lead Banker approves it.
        loan = bdb.create_loan(interaction.user.id, float(amount), LOAN_APR, None,
                               status="pending", term_days=days)
        await interaction.followup.send(
            embed=_embed("🕒 Loan request submitted",
                         f"Requested **{fmt(amount)}** {COIN} for **{days} days** "
                         f"(request #{loan['id']}).\n"
                         f"A Lead Banker has to approve it — nothing has been paid out yet. "
                         f"Check with `/loan status`.",
                         color=0xF1C40F),
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
    await interaction.followup.send(
        embed=_embed("📈 Loan approved",
                     f"Borrowed **{fmt(amount)}** {COIN} (loan #{loan['id']}).\n"
                     f"APR {LOAN_APR*100:.1f}% · due in {days} days.\n"
                     f"Repay with `/loan repay`. Wallet: {fmt(res['coins'])} {COIN}",
                     color=0xE67E22),
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
            await interaction.followup.send("You have no active loans. 🎉", ephemeral=True)
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
        embed=_embed("✅ Repayment applied",
                     f"Repaid **{fmt(pay)}** {COIN}.\n"
                     f"Remaining debt: **{fmt(new_debt)}** {COIN}\n"
                     f"Wallet: {fmt(res['coins'])} {COIN}"),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(
        f"✅ {interaction.user.mention} repaid **{fmt(pay)}** {COIN}. "
        f"Remaining debt: {fmt(new_debt)} {COIN}."))


@loan_group.command(name="status", description="See your outstanding loans")
async def loan_status(interaction: discord.Interaction):
    if not await ensure_account(interaction, write=False):
        return
    loans = bdb.get_active_loans(interaction.user.id)
    pending = bdb.get_pending_loans(interaction.user.id)
    limit = credit_limit_for(interaction.user.id)
    if not loans and not pending:
        await interaction.response.send_message(
            f"No active loans. 🎉\nYou can borrow up to **{fmt(limit)}** {COIN}.", ephemeral=True)
        return
    lines = []
    for p in pending:
        lines.append(f"#{p['id']}: **{fmt(p['principal'])}** {COIN} — 🕒 *awaiting approval "
                     f"({p['term_days']}d)*")
    if pending and loans:
        lines.append("")
    overdue_any = False
    for l in loans:
        due_raw = l["due_at"] or ""
        due = due_raw[:10]
        d = _parse_iso(due_raw)
        is_overdue = bool(d and utcnow() > d)
        overdue_any = overdue_any or is_overdue
        tag = " ⚠️ **OVERDUE**" if is_overdue else ""
        lines.append(f"#{l['id']}: **{fmt(l['balance'])}** {COIN} owed "
                     f"(borrowed {fmt(l['principal'])}, APR {l['apr']*100:.1f}%, due {due}){tag}")
    if overdue_any:
        lines.append(f"\n⚠️ Overdue loans accrue an extra {LOAN_OVERDUE_EXTRA_APR*100:.0f}% APR until repaid.")
    total = bdb.total_debt(interaction.user.id)
    await interaction.response.send_message(
        embed=_embed("📋 Your loans",
                     "\n".join(lines)
                     + f"\n\n**Total debt: {fmt(total)} {COIN}**"
                     + f"\nCredit limit: {fmt(limit)} {COIN} "
                       f"(headroom {fmt(max(0, limit - total))} {COIN})",
                     color=0xE67E22),
        ephemeral=True,
    )



savings_group = app_commands.Group(name="savings", description="Savings info")


@savings_group.command(name="rate", description="See current savings & loan rates")
async def savings_rate(interaction: discord.Interaction):
    daily = SAVINGS_APR / 365
    await interaction.response.send_message(
        embed=_embed("💹 Rates",
                     f"**Savings APR:** {SAVINGS_APR*100:.2f}% (~{daily*100:.4f}%/day, compounded daily)\n"
                     f"**Loan APR:** {LOAN_APR*100:.2f}%\n"
                     f"Interest is applied once every 24h."),
        ephemeral=True,
    )



bond_group = app_commands.Group(name="bond", description="Lock coins in fixed-term bonds for higher interest")


async def _term_autocomplete(interaction: discord.Interaction, current: str):
    out = []
    for days, apr in BOND_TERMS.items():
        out.append(app_commands.Choice(
            name=f"{days} days — {apr*100:.1f}% APR", value=days))
    return out[:25]


@bond_group.command(name="rates", description="See available bond terms and rates")
async def bond_rates(interaction: discord.Interaction):
    if not BOND_TERMS:
        await interaction.response.send_message("No bond products are configured.", ephemeral=True)
        return
    lines = []
    for days, apr in BOND_TERMS.items():
        ex = _bond_payout(1000, apr, days)
        lines.append(f"**{days} days** — {apr*100:.1f}% APR · 1,000 {COIN} → **{fmt(ex)}** {COIN} at maturity")
    note = ("\nEarly redemption returns your principal"
            + (f" minus a {BOND_EARLY_PENALTY_PCT*100:.0f}% penalty" if BOND_EARLY_PENALTY_PCT else "")
            + " but forfeits the interest.")
    await interaction.response.send_message(
        embed=_embed("📜 Bond rates", "\n".join(lines) + note, color=0x9B59B6),
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
        avail = ", ".join(f"{d}d" for d in BOND_TERMS) or "none"
        await interaction.response.send_message(
            f"❌ `{term}` isn't an available term. Available: {avail}. See `/bond rates`.",
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
    await interaction.followup.send(
        embed=_embed("📜 Bond purchased",
                     f"Locked **{fmt(amount)}** {COIN} for **{term} days** at {apr*100:.1f}% APR "
                     f"(bond #{bond['id']}).\n"
                     f"Matures {matures[:10]} → pays out **{fmt(payout)}** {COIN}.\n"
                     f"Wallet: {fmt(res['coins'])} {COIN}",
                     color=0x9B59B6),
        ephemeral=True,
    )
    asyncio.create_task(_log_activity(
        f"📜 {interaction.user.mention} bought a {term}-day bond for **{fmt(amount)}** {COIN} "
        f"(bond #{bond['id']})."))


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
    lines = []
    for b in bonds:
        matured = now.isoformat() >= b["matures_at"]
        flag = "✅ **MATURED — redeem now**" if matured else f"matures {b['matures_at'][:10]}"
        lines.append(f"#{b['id']}: {fmt(b['principal'])} {COIN} @ {b['apr']*100:.1f}% "
                     f"({b['term_days']}d) → {fmt(b['payout'])} {COIN} · {flag}")
    locked = bdb.total_bonds_value(interaction.user.id)
    await interaction.response.send_message(
        embed=_embed("📜 Your bonds",
                     "\n".join(lines) + f"\n\n**Value at maturity: {fmt(locked)} {COIN}**",
                     color=0x9B59B6),
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
        await interaction.followup.send("❌ That bond isn't yours or doesn't exist.", ephemeral=True)
        return
    if bond["status"] != "active":
        await interaction.followup.send("❌ That bond has already been redeemed.", ephemeral=True)
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
        kind_note = f"early redemption (interest forfeited{f', −{fmt(penalty)} penalty' if penalty else ''})"

    # Everything below runs under the user's lock. The split between "goes to
    # debt" and "goes to the wallet" is decided before an HTTP round trip and
    # acted on after it; without the lock, a collections pass or a /loan repay
    # landing in that window could clear the debt, leaving the garnished share
    # applied to nothing and simply destroyed.
    async with _user_lock(interaction.user.id):
        if bdb.get_bond(bond_id)["status"] != "active":
            await interaction.followup.send("❌ That bond has already been redeemed.", ephemeral=True)
            return

        # A bond payout is money the bank is already holding, so overdue debt is
        # settled out of it before the rest reaches the wallet. Only OVERDUE debt
        # is garnished — a loan that's merely outstanding is left alone.
        garnish = 0
        if GARNISH_BOND_PAYOUTS:
            garnish = int(min(amount, math.floor(_overdue_debt(interaction.user.id))))
        to_wallet = amount - garnish

        if not bdb.claim_bond(bond_id):
            await interaction.followup.send("❌ That bond has already been redeemed.", ephemeral=True)
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

    body = f"Bond #{bond_id} — {kind_note}.\nPayout: **{fmt(amount)}** {COIN}\n"
    if applied:
        body += (f"⚠️ **{fmt(applied)}** {COIN} went straight to your overdue debt.\n"
                 f"Remaining debt: {fmt(bdb.total_debt(interaction.user.id))} {COIN}\n")
    if shortfall > 0:
        body += f"**{fmt(shortfall)}** {COIN} went to your savings.\n"
    body += f"To your wallet: **{fmt(to_wallet)}** {COIN}"
    if res:
        body += f"\nWallet: {fmt(res['coins'])} {COIN}"
    await interaction.followup.send(
        embed=_embed("💵 Bond redeemed", body, color=0x9B59B6), ephemeral=True)

    asyncio.create_task(_log_activity(
        f"💵 {interaction.user.mention} redeemed bond #{bond_id} for **{fmt(amount)}** {COIN} "
        f"({kind_note})."
        + (f" **{fmt(applied)}** {COIN} garnished against overdue debt." if applied else "")))



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
        label = f"{m['name']} ({m['market_id']}) — {m['price']:,.2f}"
        if cur in m["market_id"].lower() or cur in m["name"].lower():
            out.append(app_commands.Choice(name=label[:100], value=m["market_id"]))
    return out[:25]


@invest_group.command(name="list", description="List public markets you can invest in")
async def invest_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    markets = await _safe(interaction, client_rs.list_stocks())
    if markets is None:
        return
    if not markets:
        await interaction.followup.send("No public markets right now.", ephemeral=True)
        return
    lines = [f"**{m['name']}** `{m['market_id']}` — {m['price']:,.2f} {COIN}/share "
             f"(P/E {m['pe']:.1f})" for m in markets]
    await interaction.followup.send(
        embed=_embed("📈 Public markets", "\n".join(lines)), ephemeral=True)


@invest_group.command(name="buy", description="Buy shares (paid from your wallet)")
@app_commands.describe(market="The market to invest in", shares="How many shares")
@app_commands.autocomplete(market=_market_autocomplete)
async def invest_buy(interaction: discord.Interaction, market: str,
                     shares: app_commands.Range[int, 1, 1_000_000]):
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    res = await _safe(interaction, client_rs.stock_buy(
        interaction.user.id, market, shares, name=interaction.user.display_name))
    if res is None:
        return
    if res.get("ok"):
        bdb.log(interaction.user.id, "stock_buy", shares, f"{market}")
        asyncio.create_task(_log_activity(
            f"📈 {interaction.user.mention} bought {shares} shares of `{market}`."))
    await interaction.followup.send(res.get("message", "Done."), ephemeral=True)


@invest_group.command(name="sell", description="Sell shares back to the market")
@app_commands.describe(market="The market you hold", shares="How many shares")
@app_commands.autocomplete(market=_market_autocomplete)
async def invest_sell(interaction: discord.Interaction, market: str,
                      shares: app_commands.Range[int, 1, 1_000_000]):
    if not await ensure_account(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    res = await _safe(interaction, client_rs.stock_sell(
        interaction.user.id, market, shares, name=interaction.user.display_name))
    if res is None:
        return
    if res.get("ok"):
        bdb.log(interaction.user.id, "stock_sell", shares, f"{market}")
        asyncio.create_task(_log_activity(
            f"📉 {interaction.user.mention} sold {shares} shares of `{market}`."))
    await interaction.followup.send(res.get("message", "Done."), ephemeral=True)


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
    lines, total = [], 0.0
    for h in holdings:
        total += h["value"]
        pl = h["value"] - h["cost_basis"]
        arrow = "🟢" if pl >= 0 else "🔴"
        lines.append(f"**{h['market_id']}**: {h['shares']:,.0f} @ {h['price']:,.2f} "
                     f"= {h['value']:,.0f} {COIN} {arrow} {pl:+,.0f}")
    await interaction.followup.send(
        embed=_embed("📊 Your portfolio",
                     "\n".join(lines) + f"\n\n**Total value: {fmt(total)} {COIN}**"),
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

    wallet = "—"
    if client_rs is not None:
        try:
            wallet = f"{fmt((await client_rs.get_balance(member.id))['coins'])} {COIN}"
        except RestockerError as e:
            wallet = f"⚠️ {e}"

    status = "🟢 open" if acct["opted_in"] else "🔒 closed"
    if acct.get("frozen"):
        status += f" · 🧊 FROZEN ({acct.get('frozen_reason') or 'no reason given'})"

    e = _embed(f"🔍 {member.display_name}", f"`{member.id}` · {status}", color=0x3498DB)
    e.add_field(name="Wallet", value=wallet, inline=True)
    e.add_field(name="Savings", value=f"{fmt(sav)} {COIN}", inline=True)
    e.add_field(name="Bonds", value=f"{fmt(bonds)} {COIN}", inline=True)
    e.add_field(name="Debt", value=f"{fmt(debt)} {COIN}", inline=True)
    e.add_field(name="Credit limit",
                value=f"{fmt(credit_limit_for(member.id))} {COIN}"
                      + (" *(override)*" if acct.get("credit_limit") is not None else ""),
                inline=True)
    e.add_field(name="Overdue loans", value=str(len(overdue)), inline=True)
    e.add_field(name="Track record",
                value=(f"repaid {hist['repaid_count']} · late {hist['late_count']} · "
                       f"written off {hist['written_off_count']} · denied {hist['denied_count']}"),
                inline=False)
    if pending:
        e.add_field(name="Awaiting approval",
                    value="\n".join(f"#{p['id']} — {fmt(p['principal'])} {COIN} ({p['term_days']}d)"
                                    for p in pending),
                    inline=False)
    e.add_field(name="Opened", value=str(acct["created_at"])[:10], inline=False)
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
        f"🧊 Froze {member.mention}'s account.", ephemeral=True)
    asyncio.create_task(_log_activity(
        f"🧊 {interaction.user.mention} froze {member.mention}'s account."
        + (f" Reason: {reason}" if reason else "")))


@admin_group.command(name="unfreeze", description="Lift a freeze")
@app_commands.describe(member="Whose account")
async def admin_unfreeze(interaction: discord.Interaction, member: discord.Member):
    if not await ensure_banker(interaction):
        return
    bdb.set_frozen(member.id, False)
    bdb.log(member.id, "account_unfrozen", 0, f"by {interaction.user.id}")
    await interaction.response.send_message(
        f"🔓 Unfroze {member.mention}'s account.", ephemeral=True)
    asyncio.create_task(_log_activity(
        f"🔓 {interaction.user.mention} unfroze {member.mention}'s account."))


@admin_group.command(name="creditlimit", description="Override how much a member may borrow")
@app_commands.describe(member="Whose limit", limit="New limit, or -1 to clear the override")
async def admin_creditlimit(interaction: discord.Interaction, member: discord.Member,
                            limit: app_commands.Range[int, -1, 100_000_000]):
    if not await ensure_banker(interaction):
        return
    if limit < 0:
        bdb.set_credit_limit(member.id, None)
        msg = (f"↩️ Cleared {member.mention}'s override — back to the earned limit "
               f"(**{fmt(credit_limit_for(member.id))}** {COIN}).")
    else:
        bdb.set_credit_limit(member.id, limit)
        msg = f"💳 Set {member.mention}'s credit limit to **{fmt(limit)}** {COIN}."
    bdb.log(member.id, "credit_limit_set", max(0, limit), f"by {interaction.user.id}")
    await interaction.response.send_message(msg, ephemeral=True)
    asyncio.create_task(_log_activity(f"💳 {interaction.user.mention}: {msg}"))


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
            f"❌ They only have {fmt(cur)} {COIN} in savings.", ephemeral=True)
        return
    if amount > 0:
        bdb.add_savings(member.id, amount)
    new = bdb.get_savings(member.id)["balance"]
    bdb.log(member.id, "admin_savings_adjust", amount, f"by {interaction.user.id}: {reason}")
    await interaction.response.send_message(
        f"✅ {'Credited' if amount > 0 else 'Debited'} **{fmt(abs(amount))}** {COIN} "
        f"{'to' if amount > 0 else 'from'} {member.mention}'s savings. New balance: "
        f"**{fmt(new)}** {COIN}.", ephemeral=True)
    asyncio.create_task(_log_activity(
        f"🛠️ {interaction.user.mention} adjusted {member.mention}'s savings by "
        f"**{amount:+,}** {COIN} — {reason}"))


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
                    f"❌ Loan #{loan_id} isn't {member.display_name}'s.", ephemeral=True)
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
        f"🩹 Wrote off **{fmt(wiped)}** {COIN} for {member.mention} "
        f"(loan{'s' if len(ids) > 1 else ''} {', '.join('#' + str(i) for i in ids)}).\n"
        f"Note: a written-off loan drops their earned credit limit to 0 until you set "
        f"an override with `/admin creditlimit`.", ephemeral=True)
    asyncio.create_task(_log_activity(
        f"🩹 {interaction.user.mention} wrote off **{fmt(wiped)}** {COIN} of "
        f"{member.mention}'s debt."))


@admin_group.command(name="loans", description="Loan requests waiting for a decision")
async def admin_loans(interaction: discord.Interaction):
    if not await ensure_banker(interaction):
        return
    pending = bdb.get_pending_loans()
    if not pending:
        await interaction.response.send_message("No loan requests pending. 🎉", ephemeral=True)
        return
    lines = [f"#{p['id']} — <@{p['user_id']}> · **{fmt(p['principal'])}** {COIN} · "
             f"{p['term_days']}d · asked {str(p['requested_at'])[:10]}"
             for p in pending[:25]]
    extra = f"\n…and {len(pending) - 25} more." if len(pending) > 25 else ""
    await interaction.response.send_message(
        embed=_embed("🕒 Pending loan requests",
                     "\n".join(lines) + extra
                     + "\n\nDecide with `/admin approve` / `/admin deny`, or the buttons "
                       "on the proposal message.",
                     color=0xF1C40F),
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
        await interaction.response.send_message("Nobody is overdue. 🎉", ephemeral=True)
        return
    now = utcnow()
    lines, total = [], 0.0
    for l in loans[:25]:
        due = _parse_iso(l["due_at"])
        late = (now - due).days if due else 0
        total += float(l["balance"])
        sav = bdb.get_savings(l["user_id"])["balance"]
        lines.append(f"#{l['id']} <@{l['user_id']}> — **{fmt(l['balance'])}** {COIN} · "
                     f"{late}d late · savings {fmt(sav)} {COIN}")
    extra = f"\n…and {len(loans) - 25} more." if len(loans) > 25 else ""
    await interaction.response.send_message(
        embed=_embed("⚠️ Overdue loans",
                     "\n".join(lines) + extra
                     + f"\n\n**Total overdue: {fmt(total)} {COIN}** across {len(loans)} loan(s)."
                     + (f"\nSavings are seized automatically after "
                        f"{COLLECT_GRACE_DAYS:g} days overdue."
                        if COLLECT_FROM_SAVINGS else
                        "\nAutomatic collection is **off** (`COLLECT_FROM_SAVINGS=0`)."),
                     color=0xE74C3C),
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
        await interaction.followup.send(f"❌ Collections failed: {e}", ephemeral=True)
        return
    after = sum(float(l["balance"]) for l in bdb.overdue_loans())
    await interaction.followup.send(
        f"🏛️ Collections done. Overdue debt {fmt(before)} → **{fmt(after)}** {COIN} "
        f"(recovered {fmt(max(0.0, before - after))} {COIN}).", ephemeral=True)


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
            f"❌ {member.mention} still owes **{fmt(debt)}** {COIN}. Collect or "
            f"`/admin forgive` it first.", ephemeral=True)
        return
    bdb.close_account(member.id)
    bdb.log(member.id, "account_closed", 0, f"forced by {interaction.user.id}: {reason}")
    sav = bdb.get_savings(member.id)["balance"]
    note = (f"\n⚠️ They still have **{fmt(sav)}** {COIN} in savings and "
            f"{len(bdb.get_bonds(member.id, 'active'))} active bond(s) — reopening the "
            f"account with `/bank open` restores access to them." if sav > 0 else "")
    await interaction.response.send_message(
        f"🔒 Closed {member.mention}'s account.{note}", ephemeral=True)
    asyncio.create_task(_log_activity(
        f"🔒 {interaction.user.mention} force-closed {member.mention}'s account."
        + (f" Reason: {reason}" if reason else "")))


@admin_group.command(name="stats", description="Bank-wide totals")
async def admin_stats(interaction: discord.Interaction):
    if not await ensure_banker(interaction):
        return
    s = bdb.bank_stats()
    e = _embed("🏦 Bank of Osentar — books", color=0x3498DB)
    e.add_field(name="Accounts",
                value=f"{s['accounts_open']} open · {s['accounts_closed']} closed · "
                      f"{s['accounts_frozen']} frozen", inline=False)
    e.add_field(name="Savings held", value=f"{fmt(s['savings_total'])} {COIN}", inline=True)
    e.add_field(name="Bonds locked", value=f"{fmt(s['bonds_locked'])} {COIN}", inline=True)
    e.add_field(name="Bond liability", value=f"{fmt(s['bonds_payout'])} {COIN}", inline=True)
    e.add_field(name="Loans out",
                value=f"{fmt(s['debt_total'])} {COIN} ({s['loans_active']} active)", inline=True)
    e.add_field(name="Pending requests", value=str(s["loans_pending"]), inline=True)
    e.add_field(name="Written off", value=f"{fmt(s['written_off_total'])} {COIN}", inline=True)
    overdue = bdb.overdue_loans()
    e.add_field(name="Overdue",
                value=f"{fmt(sum(float(l['balance']) for l in overdue))} {COIN} "
                      f"({len(overdue)} loan(s))", inline=False)
    # What the bank owes depositors vs what it's owed back.
    liabilities = float(s["savings_total"]) + float(s["bonds_payout"])
    assets = float(s["debt_total"])
    e.add_field(name="Position",
                value=f"Owed to members {fmt(liabilities)} {COIN} · owed to bank {fmt(assets)} {COIN} "
                      f"· **net {fmt(assets - liabilities)}** {COIN}", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@admin_group.command(name="config", description="Show the bank's effective settings")
async def admin_config(interaction: discord.Interaction):
    if not await ensure_banker(interaction):
        return
    terms = ", ".join(f"{d}d@{a*100:.1f}%" for d, a in BOND_TERMS.items()) or "none"
    e = _embed("⚙️ Effective config", color=0x95A5A6)
    e.add_field(name="Rates",
                value=f"Savings {SAVINGS_APR*100:.2f}% · Loan {LOAN_APR*100:.2f}% "
                      f"(+{LOAN_OVERDUE_EXTRA_APR*100:.0f}% overdue)", inline=False)
    e.add_field(name="Lending",
                value=f"Approval gate **{'ON' if LOAN_REQUIRE_APPROVAL else 'OFF'}** · "
                      f"base limit {fmt(BASE_CREDIT_LIMIT)} +{fmt(CREDIT_PER_REPAID_LOAN)}/repaid "
                      f"−{fmt(CREDIT_LATE_PENALTY)}/late · hard cap {fmt(MAX_LOAN)}", inline=False)
    e.add_field(name="Collections",
                value=f"Seize savings **{'ON' if COLLECT_FROM_SAVINGS else 'OFF'}** after "
                      f"{COLLECT_GRACE_DAYS:g}d · garnish bonds "
                      f"**{'ON' if GARNISH_BOND_PAYOUTS else 'OFF'}** · announce "
                      f"**{'ON' if OVERDUE_ANNOUNCE else 'OFF'}**", inline=False)
    e.add_field(name="Bonds", value=f"{terms} · early penalty {BOND_EARLY_PENALTY_PCT*100:.0f}%",
                inline=False)
    e.add_field(name="Staff",
                value=f"{len(LEAD_BANKER_ROLE_IDS)} role(s), {len(BANK_ADMIN_USER_IDS)} user(s) "
                      f"allow-listed"
                      + ("\n⚠️ Neither is set — falling back to server Administrators."
                         if not (LEAD_BANKER_ROLE_IDS or BANK_ADMIN_USER_IDS) else ""),
                inline=False)
    e.add_field(name="Restocker",
                value=(f"`{RESTOCKER_API_URL}`" if RESTOCKER_API_URL else "⚠️ not configured"),
                inline=False)
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
            await _log_activity(
                f"⚠️ Loan #{loan_id} for <@{uid}> is **OVERDUE** — {fmt(loan['balance'])} {COIN} "
                f"owed, was due {str(loan['due_at'])[:10]}. Penalty APR now applies.")

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
        await _log_activity(
            f"🏛️ Collected **{fmt(take)}** {COIN} from <@{uid}>'s savings against overdue "
            f"loan #{loan_id}. Remaining debt: {fmt(left)} {COIN}.")


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

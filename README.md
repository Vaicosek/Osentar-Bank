# Banking bot

A standalone Discord bot that adds **banking** on top of the
[Restocker](../Restocker) Minecraft-shop economy. It runs on its **own wispbyte
server** and talks to Restocker over an authenticated HTTP API — Restocker stays
the single source of truth for the coin wallet and the stock exchange.

## What it does

| Area | Commands |
|------|----------|
| Accounts | `/bank open`, `/bank balance`, `/bank history`, `/bank close` |
| Savings  | `/bank deposit`, `/bank withdraw`, `/savings rate` |
| Payments | `/bank transfer @user amount` |
| Loans    | `/loan request`, `/loan repay`, `/loan status` |
| Bonds    | `/bond rates`, `/bond buy`, `/bond list`, `/bond redeem` |
| Investing| `/invest list`, `/invest buy`, `/invest sell`, `/invest portfolio` |
| Staff    | `/admin …` — see [Lead Banker tools](#lead-banker-tools) |

- **Savings** earn interest (default 5% APR, compounded daily) in a bank vault.
- **Loans** credit your wallet and accrue interest (default 18% APR) until repaid.
  By default they need **Lead Banker approval** first, and each member has a
  **credit limit** that grows as they repay on time.
- **Bonds** are fixed-term deposits: lock coins for a term (default 7/30/90 days)
  at a higher fixed rate; the payout (principal + simple interest) is locked in at
  purchase and paid at maturity. Early redemption returns principal but forfeits
  the interest (plus an optional penalty). Terms/rates set via `BOND_TERMS`.
- **Transfers** move coins wallet→wallet atomically.
- **Investing** proxies straight into Restocker's stock exchange, so prices,
  holdings and P/E stay consistent with `/stock` in Restocker and the website.
- An hourly loop compounds savings and loan interest from real elapsed time,
  then runs **collections** against overdue loans.

## Lending

`/loan request` doesn't hand out coins on its own. What happens:

1. The member's **credit limit** is checked. It starts at `BASE_CREDIT_LIMIT`,
   goes up `CREDIT_PER_REPAID_LOAN` for every loan they've fully repaid, down
   `CREDIT_LATE_PENALTY` for every loan that ever went overdue, and straight to
   **0** if the bank ever had to write one off. `/admin creditlimit` overrides it.
2. With `LOAN_REQUIRE_APPROVAL=1` (the default) the loan is stored as
   `pending` — *no coins move, no interest accrues, it isn't counted as debt* —
   and a proposal is posted to `LOAN_PROPOSALS_CHANNEL_ID` with **Approve** and
   **Deny** buttons plus the borrower's track record.
3. A Lead Banker clicks Approve → the limit is re-checked (debt may have moved
   since), coins are disbursed, and the clock starts from *approval*, not from
   the request. Deny closes it out. Either way the proposal message is rewritten
   with the verdict and the buttons are removed.

The buttons carry the loan ID inside their `custom_id`, so **they keep working
after a bot restart**. If the proposal post fails (bad channel, missing
permission), the request isn't stranded — `/admin loans` lists everything
pending and `/admin approve` / `/admin deny` decide it by number.

Set `LOAN_REQUIRE_APPROVAL=0` to go back to instant disbursement; the credit
limit still applies.

## Collections

Overdue loans already accrue at a penalty rate (`LOAN_APR` +
`LOAN_OVERDUE_EXTRA_APR`). On top of that, the hourly pass:

- **announces** each loan once, the first time it goes past due
  (`OVERDUE_ANNOUNCE`);
- after `COLLECT_GRACE_DAYS`, **seizes the defaulter's savings** against the
  debt (`COLLECT_FROM_SAVINGS`);
- **garnishes bond payouts**: when a bond is redeemed, overdue debt is settled
  out of the payout before the remainder reaches the wallet
  (`GARNISH_BOND_PAYOUTS`).

The **wallet is never touched** by collections. The bank can take what it's
already holding — savings and bond payouts — but doesn't reach into anyone's
pocket. All of it is local to `bank.db`, so there's no half-completed remote
call to reconcile.

`/admin overdue` lists everyone in default with how late they are and how much
savings there is to take; `/admin collect` runs a pass on demand.

## Lead Banker tools

Access: a role in `LEAD_BANKER_ROLE_IDS`, a user in `BANK_ADMIN_USER_IDS`, or —
if neither is configured — server Administrator.

| Command | What it does |
|---------|--------------|
| `/admin account @user` | Full picture: wallet, savings, bonds, debt, credit limit, track record, pending requests |
| `/admin loans` | The pending-approval queue |
| `/admin approve <id>` / `/admin deny <id>` | Decide a request by number |
| `/admin freeze @user [reason]` / `/admin unfreeze @user` | Block money movement. Frozen users can still read `/bank balance` and `/loan status` |
| `/admin creditlimit @user <n>` | Override the earned limit (`-1` clears the override) |
| `/admin savings @user <±n> <reason>` | Correct a balance, pay a bounty, levy a fine. Goes in the ledger |
| `/admin forgive @user [loan_id]` | Write off debt — no coins move, it just stops existing. Omit the ID to forgive all of it |
| `/admin overdue` | Everyone in default |
| `/admin collect` | Run a collections pass now |
| `/admin close @user [reason]` | Force-close an account (must be debt-free) |
| `/admin stats` | Bank-wide books: deposits held, loans out, bond liability, net position |
| `/admin config` | The settings actually in effect, with warnings for anything unconfigured |

Every admin action is written to the ledger and posted to `BOT_LOG_CHANNEL_ID`.

## Architecture

```
 ┌────────────────────┐         HTTPS + X-Bank-Token        ┌──────────────────────┐
 │   Banking bot       │  ───────────────────────────────▶  │  Restocker server     │
 │  (this server)      │   /api/bank/balance                │  Restocker_web.py     │
 │                     │   /api/bank/adjust                  │   └─ bank_api.py      │
 │  bank.db            │   /api/bank/transfer                │  Restocker_main.py    │
 │   • accounts        │   /api/bank/stock/buy|sell          │   add_coins/deduct    │
 │   • savings         │   /api/bank/portfolio               │   _exec_stock_buy/sell│
 │   • loans           │ ◀───────────────────────────────   │  restocker.db (wallet,│
 │   • ledger          │            JSON {ok,...}            │   stocks = truth)     │
 └────────────────────┘                                     └──────────────────────┘
```

- **`bank.db`** (owned here) holds savings, loans and an audit ledger only.
- The **coin wallet** and **stock holdings** live in Restocker; this bot never
  stores them, it asks Restocker every time.
- Every money move debits/credits the wallet on Restocker *first*, then records
  the local side — and refunds/rejects if the remote call fails — so the two
  systems can't silently drift.

## Files

| File | Role |
|------|------|
| `main.py` | Tiny launcher. |
| `bank_main.py` | The Discord bot — all slash commands, the interest loop, collections. |
| `bank_db.py` | SQLite layer for `bank.db` (accounts, savings, loans, bonds, ledger). |
| `restocker_client.py` | Async HTTP client for Restocker's `/api/bank/*` API. |
| `doctor.py` | Preflight check — run before starting on a new server. |
| `tests/` | Regression tests. `python -m pytest tests/ -q`, or run either file directly. |
| `requirements.txt` | `discord.py`, `aiohttp`, `python-dotenv`. |
| `.env.example` | All config — copy to `.env`. |

## Setup

### 1. On the Restocker server (one time)
The integration files are already added to the Restocker project:
`bank_api.py` plus two lines in `Restocker_web.py` that register the routes.
Just set a shared secret in Restocker's `.env`:

```
BANK_API_TOKEN=<long-random-string>
```

Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Restart Restocker; you should see `🏦 Bank API ENABLED` in the logs. If the token
is empty the API is fully disabled (returns 503).

### 2. On the banking-bot server
```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env
python doctor.py          # preflight — fix every ❌ before continuing
python main.py
```

Set in `.env`:
- `BANK_DISCORD_TOKEN` — the bank bot's own Discord application token.
- `RESTOCKER_API_URL` — Restocker's public URL (e.g. the Cloudflare tunnel), no trailing slash.
- `RESTOCKER_BANK_TOKEN` — **must equal** Restocker's `BANK_API_TOKEN`.
- `BANK_GUILD_ID` — your server ID, so slash commands appear immediately
  instead of taking up to an hour to propagate globally.
- `LEAD_BANKER_ROLE_IDS` — otherwise `/admin` is Administrators-only.

`doctor.py` checks all of that plus: the DB opens and migrates, the Restocker
API is reachable *and* enabled *and* on a matching version, the token is
accepted, the Discord login works, the bot is actually in the guild, and it can
post in each configured channel. `--offline` skips the network half. It exits
non-zero if anything is broken, so it can gate a deploy script.

### 3. Deploying an update
`bank.db` migrates itself on startup — new columns are added in place, existing
rows and history are preserved, and re-running is a no-op. Just replace the
files and restart; there's no migration step to remember. Interest accrues from
real elapsed time, so downtime during a redeploy doesn't skip or double-count.

## Notes

- **User IDs match** because both bots use Discord user IDs as the key — a user's
  wallet in Restocker and their bank account here are automatically the same person.
- The API uses **best-effort idempotency keys** so a network retry won't
  double-charge.
- Interest accrues every 24h while the bot is running. Long downtime means missed
  accruals (not retroactive) — fine for a game economy; raise an issue if you want
  catch-up accrual based on elapsed time instead.
- Every brand-new `/bank open` posts a "🎫 New account — pending review" ticket
  (with ✅/❌ reactions) to `NEW_ACCOUNT_CHANNEL_ID`, for Lead Bankers to review.
  This is visibility/log only — it doesn't block the account, which is active
  immediately. Posting failures (missing channel/permissions) are logged and
  never break `/bank open` for the user.
- Loan requests post to `LOAN_PROPOSALS_CHANNEL_ID` with live Approve/Deny
  buttons — see [Lending](#lending) above.
- A **frozen** account (`/admin freeze`) can't move money — deposits,
  withdrawals, transfers, loans, bonds and trades all refuse with the reason
  the Lead Banker gave. Read-only commands still work, so the member can see
  where they stand, and a pending loan can't be approved while they're frozen.
- `/bank deposit`, `/bank withdraw`, `/bank transfer`, `/loan repay`,
  `/bond buy`, `/bond redeem`, `/invest buy`, and `/invest sell` each post a
  one-line audit entry to `BOT_LOG_CHANNEL_ID`.
- All of the above channel posts are best-effort and fire-and-forget: a
  missing channel or missing bot permission only logs a warning server-side,
  it never breaks the user's command.
- `/bank close` is a **soft delete**: it requires zero debt and zero active
  bonds, cashes out any savings to the wallet, asks for a button confirmation,
  then flips `opted_in` to 0. The account row and full ledger history are kept
  (deleting the row outright isn't possible — loans/bonds have a foreign key
  on it — and it would destroy the audit trail anyway). Running `/bank open`
  again reactivates the same account. Closures are logged to
  `BOT_LOG_CHANNEL_ID`.
- **Security:** all `/api/bank/*` calls require the `X-Bank-Token` header. Keep the
  token secret and serve Restocker over HTTPS.
```

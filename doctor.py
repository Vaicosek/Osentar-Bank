"""
doctor.py — preflight check. Run this BEFORE starting the bot on a new server:

    python doctor.py

It validates .env, opens/migrates bank.db, and (unless --offline) pings the
Restocker API and logs into Discord to confirm the token and channels work.
Nothing here writes to Discord or moves any coins — it only reads.

Exit code 0 = safe to start, 1 = something is wrong.
"""
from __future__ import annotations

import asyncio
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

OFFLINE = "--offline" in sys.argv

# Keep these in step with the fallbacks in bank_main.py, or doctor warns about
# channels that the bot is in fact perfectly happy with.
CHANNEL_DEFAULTS = {
    "NEW_ACCOUNT_CHANNEL_ID": "1518146924270587934",
    "LOAN_PROPOSALS_CHANNEL_ID": "1515925123159556111",
    "BOT_LOG_CHANNEL_ID": "1515925132051349617",
}


def channel(name: str) -> str:
    return os.getenv(name, CHANNEL_DEFAULTS.get(name, "")).strip()

OK, WARN, BAD = "✅", "⚠️ ", "❌"
_problems = 0
_warnings = 0


def ok(msg):
    print(f"{OK} {msg}")


def warn(msg):
    global _warnings
    _warnings += 1
    print(f"{WARN} {msg}")


def bad(msg):
    global _problems
    _problems += 1
    print(f"{BAD} {msg}")


def section(name):
    print(f"\n── {name} " + "─" * max(0, 60 - len(name)))


def check_env():
    section("Configuration")
    for var, hint in [
        ("BANK_DISCORD_TOKEN", "the bank bot's own Discord application token"),
        ("RESTOCKER_API_URL", "Restocker's public URL, no trailing slash"),
        ("RESTOCKER_BANK_TOKEN", "must equal BANK_API_TOKEN in Restocker's .env"),
    ]:
        if os.getenv(var, "").strip():
            ok(f"{var} is set")
        else:
            bad(f"{var} is EMPTY — {hint}")

    url = os.getenv("RESTOCKER_API_URL", "").strip()
    if url.endswith("/"):
        warn("RESTOCKER_API_URL has a trailing slash — strip it")
    if url and not url.lower().startswith("https://"):
        warn("RESTOCKER_API_URL is not HTTPS — the bank token would travel in plaintext")

    if not os.getenv("BANK_GUILD_ID", "").strip():
        warn("BANK_GUILD_ID is empty — slash commands sync globally and can take ~1h "
             "to appear. Set it to your server ID for instant sync.")
    else:
        ok("BANK_GUILD_ID set — commands will sync instantly to that server")

    staff = (os.getenv("LEAD_BANKER_ROLE_IDS", "").strip()
             or os.getenv("BANK_ADMIN_USER_IDS", "").strip())
    if staff:
        ok("Lead Banker staff configured")
    else:
        warn("Neither LEAD_BANKER_ROLE_IDS nor BANK_ADMIN_USER_IDS is set — /admin "
             "will only work for server Administrators")

    approval = os.getenv("LOAN_REQUIRE_APPROVAL", "1").strip().lower() in ("1", "true", "yes", "on")
    if approval and not channel("LOAN_PROPOSALS_CHANNEL_ID"):
        warn("LOAN_REQUIRE_APPROVAL is on but LOAN_PROPOSALS_CHANNEL_ID is empty — "
             "requests will only be visible via /admin loans")
    else:
        ok(f"Loan approval gate is {'ON' if approval else 'OFF'}")

    # Numeric sanity: a typo like SAVINGS_APR=5 (meaning 5%) pays 500%/yr.
    for var, default, ceiling in [("SAVINGS_APR", "0.05", 1.0), ("LOAN_APR", "0.18", 2.0),
                                  ("LOAN_OVERDUE_EXTRA_APR", "0.18", 2.0)]:
        try:
            v = float(os.getenv(var, default))
        except ValueError:
            bad(f"{var} is not a number")
            continue
        if v < 0:
            bad(f"{var}={v} is negative")
        elif v > ceiling:
            warn(f"{var}={v} — that's {v*100:.0f}% APR. Rates are FRACTIONS "
                 f"(0.05 = 5%), so this is probably a typo.")
        else:
            ok(f"{var} = {v*100:.2f}%")


def check_db():
    section("Database")
    try:
        import bank_db as bdb
        bdb.init_db()
        ok(f"bank.db opened and migrated at {bdb.DB_PATH}")
        s = bdb.bank_stats()
        ok(f"{s['accounts_open']} open account(s), {s['loans_active']} active loan(s), "
           f"{s['loans_pending']} pending request(s)")
        if s["loans_pending"]:
            warn(f"{s['loans_pending']} loan request(s) are waiting on a Lead Banker")
        overdue = bdb.overdue_loans()
        if overdue:
            warn(f"{len(overdue)} loan(s) are overdue "
                 f"({sum(float(l['balance']) for l in overdue):,.0f} coins)")
        return True
    except Exception as e:
        bad(f"Could not open the database: {type(e).__name__}: {e}")
        return False


async def check_restocker():
    section("Restocker API")
    url = os.getenv("RESTOCKER_API_URL", "").strip()
    token = os.getenv("RESTOCKER_BANK_TOKEN", "").strip()
    if not (url and token):
        bad("Skipped — URL or token missing")
        return
    from restocker_client import RestockerClient, RestockerError, EXPECTED_API_VERSION
    client = RestockerClient(url, token)
    try:
        h = await client.health()
        ok(f"Reached {url}")
        if not h.get("enabled"):
            bad("The bank API is reachable but DISABLED — set BANK_API_TOKEN in "
                "Restocker's .env and restart Restocker")
        else:
            ok("Bank API is enabled on the Restocker side")
        sv = h.get("version")
        if sv != EXPECTED_API_VERSION:
            warn(f"API version mismatch: server={sv}, this bot expects "
                 f"{EXPECTED_API_VERSION}. Update both sides.")
        else:
            ok(f"API version {sv} matches")
        await client.ping()
        ok("Token accepted (authenticated ping succeeded)")
        markets = await client.list_stocks()
        ok(f"{len(markets)} public market(s) visible to /invest")
    except RestockerError as e:
        bad(f"Restocker API check failed: {e}")
        if getattr(e, "status", None) in (401, 403):
            bad("→ that's an auth failure: RESTOCKER_BANK_TOKEN does not match "
                "Restocker's BANK_API_TOKEN")
    finally:
        await client.close()


async def check_discord():
    section("Discord")
    token = os.getenv("BANK_DISCORD_TOKEN", "").strip()
    if not token:
        bad("Skipped — BANK_DISCORD_TOKEN missing")
        return
    import discord

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    done = asyncio.Event()

    @client.event
    async def on_ready():
        try:
            ok(f"Logged in as {client.user} ({client.user.id})")
            gid = os.getenv("BANK_GUILD_ID", "").strip()
            if gid:
                g = client.get_guild(int(gid))
                if g:
                    ok(f"In guild '{g.name}'")
                else:
                    bad(f"The bot is NOT in guild {gid} — invite it with the "
                        f"applications.commands scope")
            for name in ("NEW_ACCOUNT_CHANNEL_ID", "LOAN_PROPOSALS_CHANNEL_ID",
                         "BOT_LOG_CHANNEL_ID"):
                cid = channel(name)
                if not cid:
                    warn(f"{name} not set — those posts are disabled")
                    continue
                try:
                    ch = client.get_channel(int(cid)) or await client.fetch_channel(int(cid))
                except Exception as e:
                    bad(f"{name}={cid} unreachable: {type(e).__name__} — wrong ID, or "
                        f"the bot can't see that channel")
                    continue
                perms = ch.permissions_for(ch.guild.me)
                missing = [p for p, need in (("view_channel", perms.view_channel),
                                             ("send_messages", perms.send_messages),
                                             ("embed_links", perms.embed_links))
                           if not need]
                if missing:
                    bad(f"{name} → #{ch.name}: missing permission(s) {', '.join(missing)}")
                else:
                    ok(f"{name} → #{ch.name} (can post)")
        finally:
            done.set()
            await client.close()

    try:
        await asyncio.wait_for(client.start(token), timeout=60)
    except asyncio.TimeoutError:
        bad("Discord login timed out")
    except discord.LoginFailure:
        bad("Discord rejected the token — BANK_DISCORD_TOKEN is wrong or was reset")
    except Exception as e:
        bad(f"Discord check failed: {type(e).__name__}: {e}")
    finally:
        if not client.is_closed():
            await client.close()


async def main():
    print("Bank bot preflight" + (" (offline mode)" if OFFLINE else ""))
    check_env()
    check_db()
    if not OFFLINE:
        await check_restocker()
        await check_discord()
    else:
        section("Network checks")
        warn("Skipped (--offline)")

    section("Result")
    if _problems:
        print(f"{BAD} {_problems} problem(s), {_warnings} warning(s) — fix the ❌ "
              f"items before starting the bot.")
        return 1
    print(f"{OK} No blocking problems ({_warnings} warning(s)). "
          f"Start the bot with: python main.py")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""
DTF Stakeholders League — Telegram bot.

Run with: python main.py
Requires DTF_BOT_TOKEN + your Telegram ID in ADMIN_IDS (config.py).

Design notes:
- All admin commands only work when DMed to the bot privately — nothing
  admin-related is ever visible in the group. Public announcements
  (challenge open/close, weekly winners) are posted to the group
  automatically from the DM commands, via the stored group_chat_id.
- Challenges are independent rows with their own start/end time, so
  several can be scheduled or live at once. A JobQueue job opens and
  closes each one automatically; on restart, schedules are rebuilt from
  the DB so a reboot never loses timing.
- The bot calculates payouts (wallet + $DTF amount) but does not send
  on-chain transactions itself — see README for why.
"""

import logging
import re
import re
import time
from datetime import datetime, timezone

from telegram import Update, BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.helpers import escape_markdown

import db
from config import (
    BOT_TOKEN,
    ADMIN_IDS,
    CATEGORIES,
    TIERS,
    DAILY_CATEGORY_CAP,
    LEADERBOARD_SIZE,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DURATION_RE = re.compile(r"^(\d+)([hdw])$", re.IGNORECASE)
UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 604800}


# --- helpers ----------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def require_admin_dm(update: Update) -> bool:
    """Returns True if this is a valid private admin call. Otherwise
    replies with guidance (only the admin themselves will ever see this,
    since non-admins get a generic 'admins only' with no further detail)
    and returns False."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Admins only.")
        return False
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Admin commands only work in a DM with me — message me privately."
        )
        return False
    return True


def touch_user(update: Update):
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.full_name or "")


def esc(text) -> str:
    """Escape any user-controlled text before it goes into a message sent
    with parse_mode="Markdown" — a stray *, _, or ` in a display name,
    challenge title, or note would otherwise make Telegram reject the
    whole message with no visible error."""
    return escape_markdown(str(text), version=1)


def format_user(username: str, display_name: str) -> str:
    if username:
        return f"@{esc(username)}"
    return esc(display_name) if display_name else "Unknown"


def parse_delay_seconds(text: str) -> int:
    if text.lower() == "now":
        return 0
    m = DURATION_RE.match(text.strip())
    if not m:
        raise ValueError(f"Invalid time value '{text}'. Use formats like now, 2h, 1d, 1w.")
    n, unit = m.groups()
    return int(n) * UNIT_SECONDS[unit.lower()]


def category_help_text() -> str:
    lines = []
    for key, (emoji, label, tiers) in CATEGORIES.items():
        tier_str = " / ".join(f"{t}:{tiers[t]}" for t in TIERS)
        lines.append(f"{emoji} `{key}` — {label} ({tier_str})")
    return "\n".join(lines)


async def get_group_chat_id(context: ContextTypes.DEFAULT_TYPE):
    raw = db.get_setting("group_chat_id", "")
    return int(raw) if raw else None


# --- public commands ----------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    await update.message.reply_text(
        "Welcome to the *DTF Stakeholders League* 🏆\n\n"
        "Earn Stakeholder Points for meaningful contributions to the "
        "$DTF community — knowledge, analysis, predictions, content, and more.\n\n"
        "If you want to be eligible for $DTF reward payouts, DM me "
        "`/register <wallet address> <twitter handle>` first.\n\n"
        "Use /rules to see how points work, /leaderboard for rankings, "
        "and /mypoints to check your own total.",
        parse_mode="Markdown",
    )


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*DTF Stakeholders League — Point Categories*\n\n"
        + category_help_text()
        + "\n\nWeekly challenge wins pay out a fixed $DTF amount right away. "
        "All other points — across every category and challenge — build "
        "one cumulative monthly total. At month end, the top scorers on "
        "that leaderboard share the monthly $DTF reward pool. You must be "
        "registered (`/register <wallet> <twitter>` in DM) to receive payouts.\n\n"
        "Commands: /leaderboard /mypoints /mystats /listchallenges /submit /register",
        parse_mode="Markdown",
    )


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Please DM me to register — your wallet address shouldn't be posted in the group."
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /register <wallet address> <twitter handle or link>\n\n"
            "Your Telegram username/ID is already on file automatically — "
            "this just adds your wallet and Twitter so your entries and "
            "contest activity can be tracked and credited properly."
        )
        return
    wallet = context.args[0]
    twitter = " ".join(context.args[1:])

    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", wallet):
        await update.message.reply_text(
            "That doesn't look like a valid wallet address — expected an EVM "
            "address in the form 0x followed by 40 hex characters. Double-check "
            "and try again."
        )
        return

    existing_owner = db.find_user_by_wallet(wallet)
    if existing_owner and existing_owner["telegram_id"] != update.effective_user.id:
        await update.message.reply_text(
            "That wallet is already registered to a different account. If this is "
            "a mistake, contact an admin — duplicate wallets aren't allowed since "
            "they'd conflict in the payout list."
        )
        return

    db.register_wallet(update.effective_user.id, wallet, twitter)
    await update.message.reply_text(
        f"✅ Registered!\nWallet: `{esc(wallet)}`\nTwitter: {esc(twitter)}\n\n"
        "You're now eligible for $DTF reward payouts based on your points.",
        parse_mode="Markdown",
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    monthly = True
    if context.args and context.args[0].lower() in ("all", "alltime", "all-time"):
        monthly = False

    rows = db.get_leaderboard(monthly=monthly, limit=LEADERBOARD_SIZE)
    if not rows:
        await update.message.reply_text("No points awarded yet — be the first!")
        return

    title = "📅 Monthly Leaderboard" if monthly else "🏛️ All-Time Leaderboard"
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"*{title}*\n"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        who = format_user(r["username"], r["display_name"])
        flag = "" if r["registered"] else " ⚠️ _unregistered_"
        lines.append(f"{prefix} {who} — {r['points']} pts{flag}")

    suffix = "\n\nUse `/leaderboard all` for all-time." if monthly else ""
    await update.message.reply_text("\n".join(lines) + suffix, parse_mode="Markdown")


async def mypoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    u = db.get_user(update.effective_user.id)
    if not u:
        await update.message.reply_text("You haven't earned any points yet.")
        return
    reg = "✅ Registered" if u["registered"] else "⚠️ Not registered — DM `/register <wallet> <twitter>` to be payout-eligible"
    await update.message.reply_text(
        f"🏆 Total: *{u['total_points']}* pts\n"
        f"📅 This month: *{u['monthly_points']}* pts\n"
        f"{reg}",
        parse_mode="Markdown",
    )


async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    rows = db.get_user_breakdown(update.effective_user.id)
    if not rows:
        await update.message.reply_text("No point history yet.")
        return
    lines = ["*Your Point Breakdown*\n"]
    for r in rows:
        emoji, label = CATEGORIES.get(r["category"], ("•", r["category"]))[:2]
        lines.append(f"{emoji} {label}: {r['total']} pts ({r['events']}x)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_build_status_digest(), parse_mode="Markdown")


def _build_status_digest() -> str:
    scheduled = db.get_challenges_by_status("scheduled")
    open_ones = db.get_challenges_by_status("open")
    now = int(time.time())

    lines = ["*📊 DTF Stakeholders League — Status*\n"]

    if open_ones:
        lines.append("*🟢 Live challenges*")
        for c in open_ones:
            remaining = max(0, c["end_time"] - now)
            hrs = remaining // 3600
            entries = db.get_submission_count(c["id"])
            lines.append(
                f"#{c['id']} — {esc(c['title'])}\n"
                f"  Deadline: ~{hrs}h left · {entries} entries so far\n"
                f"  Enter: `/submit {c['id']} <entry>`"
            )
    else:
        lines.append("*🟢 Live challenges*\nNone right now.")

    if scheduled:
        lines.append("\n*🕒 Coming up*")
        for c in scheduled:
            start_str = datetime.fromtimestamp(c["start_time"], tz=timezone.utc).strftime("%b %d, %H:%M UTC")
            lines.append(f"#{c['id']} — {esc(c['title'])} (opens {start_str})")

    lines.append(f"\n👥 Registered participants: {db.get_registered_count()}")
    lines.append("📜 Full rules: /rules")
    return "\n".join(lines)


async def _status_digest_job(context: ContextTypes.DEFAULT_TYPE):
    group_chat_id = db.get_setting("group_chat_id", "")
    if not group_chat_id:
        return
    await context.bot.send_message(
        chat_id=int(group_chat_id), text=_build_status_digest(), parse_mode="Markdown"
    )


async def postupdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-triggered: post the status digest to the group right now."""
    if not await require_admin_dm(update):
        return
    group_chat_id = db.get_setting("group_chat_id", "")
    if not group_chat_id:
        await update.message.reply_text("No announcement group set yet — run /setgroup inside your group first.")
        return
    await context.bot.send_message(
        chat_id=int(group_chat_id), text=_build_status_digest(), parse_mode="Markdown"
    )
    await update.message.reply_text("Status digest posted to the group.")


async def setupdateinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if not context.args:
        current = db.get_setting("status_update_hours", "12")
        await update.message.reply_text(
            f"Auto status updates currently post every {current}h (0 = off).\n"
            "Usage: /setupdateinterval <hours>"
        )
        return
    try:
        hours = float(context.args[0])
        if hours < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Must be a non-negative number of hours.")
        return
    db.set_setting("status_update_hours", hours)
    _reschedule_status_digest(context.application.job_queue, hours)
    if hours == 0:
        await update.message.reply_text("Automatic status updates turned off. Use /postupdate to post manually anytime.")
    else:
        await update.message.reply_text(f"Automatic status updates will now post every {hours}h.")


def _reschedule_status_digest(job_queue, hours: float):
    for job in job_queue.get_jobs_by_name("status_digest"):
        job.schedule_removal()
    if hours and hours > 0:
        job_queue.run_repeating(_status_digest_job, interval=hours * 3600, first=hours * 3600, name="status_digest")


async def listchallenges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scheduled = db.get_challenges_by_status("scheduled")
    open_ones = db.get_challenges_by_status("open")
    if not scheduled and not open_ones:
        await update.message.reply_text("No challenges scheduled or open right now.")
        return

    now = int(time.time())
    lines = []
    if open_ones:
        lines.append("*🟢 Live now*")
        for c in open_ones:
            remaining = max(0, c["end_time"] - now)
            hrs = remaining // 3600
            lines.append(f"#{c['id']} — {esc(c['title'])} (closes in ~{hrs}h)\n_{esc(c['description'])}_")
    if scheduled:
        lines.append("\n*🕒 Scheduled*")
        for c in scheduled:
            start_str = datetime.fromtimestamp(c["start_time"], tz=timezone.utc).strftime("%b %d, %H:%M UTC")
            lines.append(f"#{c['id']} — {esc(c['title'])} (opens {start_str})")

    lines.append("\nUse `/submit <id> <entry>` on a live challenge.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    u = db.get_user(update.effective_user.id)
    if not u or not u["registered"]:
        await update.message.reply_text(
            "You need to register first — DM me `/register <wallet address> <twitter handle>` "
            "so you're eligible if you win."
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /submit <challenge_id> <your entry text or link>")
        return
    try:
        challenge_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Challenge ID must be a number — see /listchallenges.")
        return
    challenge = db.get_challenge(challenge_id)
    if not challenge or challenge["status"] != "open":
        await update.message.reply_text("That challenge isn't open right now — see /listchallenges.")
        return
    content = " ".join(context.args[1:])
    _, was_update = db.add_or_update_submission(challenge_id, update.effective_user.id, content)
    if was_update:
        await update.message.reply_text("Your submission for this challenge was updated. ✅")
    else:
        await update.message.reply_text("Submission received — a mod will review it. ✅")


async def submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only, DM-only: list every submission for a challenge so you
    can actually review entries before awarding points."""
    if not await require_admin_dm(update):
        return
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /submissions <challenge_id> [oldest]\n"
            "Add \"oldest\" to review earliest-first — useful for judging "
            "\"first N correct\" style challenges."
        )
        return
    try:
        challenge_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Challenge ID must be a number — see /listchallenges.")
        return

    oldest_first = len(context.args) > 1 and context.args[1].lower() == "oldest"

    challenge = db.get_challenge(challenge_id)
    if not challenge:
        await update.message.reply_text(f"No challenge #{challenge_id} found.")
        return

    entries = db.get_submissions_for_challenge(challenge_id, oldest_first=oldest_first)
    if not entries:
        await update.message.reply_text(
            f"No submissions yet for Challenge #{challenge_id} — {esc(challenge['title'])}."
        )
        return

    order_note = " (oldest first)" if oldest_first else ""
    header = f"*Submissions for #{challenge_id} — {esc(challenge['title'])}*{order_note} ({len(entries)} total)\n"
    blocks = [header]
    for e in entries:
        who = format_user(e["username"], e["display_name"] or str(e["telegram_id"]))
        when = datetime.fromtimestamp(e["created_at"], tz=timezone.utc).strftime("%b %d, %H:%M UTC")
        status_flag = {"approved": " ✅", "rejected": " ❌"}.get(e["status"], "")
        blocks.append(
            f"— {who} [id {e['id']}]{status_flag} ({when}):\n{esc(e['content'])}"
        )

    # Telegram messages cap at 4096 chars — chunk if there are a lot of entries.
    message = "\n\n".join(blocks)
    for i in range(0, len(message), 3800):
        await update.message.reply_text(message[i:i + 3800], parse_mode="Markdown")


async def _set_submission_status(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str, label: str):
    if not await require_admin_dm(update):
        return
    if len(context.args) < 1:
        await update.message.reply_text(f"Usage: /{label} <submission_id> — get IDs from /submissions <challenge_id>")
        return
    try:
        submission_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Submission ID must be a number.")
        return
    sub = db.get_submission(submission_id)
    if not sub:
        await update.message.reply_text(f"No submission #{submission_id} found.")
        return
    db.set_submission_status(submission_id, status)
    await update.message.reply_text(f"Submission #{submission_id} marked {status}. ✅")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_submission_status(update, context, "approved", "approve")


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_submission_status(update, context, "rejected", "reject")


# --- scheduling internals ------------------------------------------------

async def _open_challenge_job(context: ContextTypes.DEFAULT_TYPE):
    challenge_id = context.job.data["challenge_id"]
    challenge = db.get_challenge(challenge_id)
    if not challenge or challenge["status"] != "scheduled":
        return
    db.set_challenge_status(challenge_id, "open")
    if challenge["chat_id"]:
        await context.bot.send_message(
            chat_id=challenge["chat_id"],
            text=(
                f"🏆 *Challenge #{challenge_id} is now live!*\n\n"
                f"*{esc(challenge['title'])}*\n{esc(challenge['description'])}\n\n"
                f"Enter with `/submit {challenge_id} <your entry>`"
            ),
            parse_mode="Markdown",
        )


async def _close_challenge_job(context: ContextTypes.DEFAULT_TYPE):
    challenge_id = context.job.data["challenge_id"]
    challenge = db.get_challenge(challenge_id)
    if not challenge or challenge["status"] != "open":
        return
    db.set_challenge_status(challenge_id, "closed")
    if challenge["chat_id"]:
        await context.bot.send_message(
            chat_id=challenge["chat_id"],
            text=(
                f"⏱️ Submissions for *Challenge #{challenge_id} — {esc(challenge['title'])}* "
                "are now closed. Winner announcement coming soon!"
            ),
            parse_mode="Markdown",
        )


def _schedule_challenge_jobs(job_queue, challenge_id: int, start_time: int, end_time: int):
    now = time.time()
    open_delay = max(0, start_time - now)
    close_delay = max(0, end_time - now)
    job_queue.run_once(_open_challenge_job, when=open_delay, data={"challenge_id": challenge_id},
                        name=f"open_{challenge_id}")
    job_queue.run_once(_close_challenge_job, when=close_delay, data={"challenge_id": challenge_id},
                        name=f"close_{challenge_id}")


# --- admin commands (DM-only) --------------------------------------------

async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Special case: run this ONCE inside the group itself so the bot
    knows where to post public announcements."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run /setgroup inside the group chat, not in DM.")
        return
    db.set_setting("group_chat_id", str(update.effective_chat.id))
    await update.message.reply_text("This group is now set as the announcement channel. ✅")


async def newchallenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /newchallenge <start_delay> <duration> <title> | <description>\n"
            "start_delay: now, 2h, 1d, 1w\n"
            "duration: 12h, 3d, 1w\n\n"
            "Example:\n"
            "/newchallenge now 3d Treasury Thread | Write a thread explaining DTF's fee split"
        )
        return

    group_chat_id = await get_group_chat_id(context)
    if not group_chat_id:
        await update.message.reply_text(
            "No announcement group set yet — go run /setgroup inside your group chat first."
        )
        return

    try:
        start_delay = parse_delay_seconds(context.args[0])
        duration = parse_delay_seconds(context.args[1])
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    if duration <= 0:
        await update.message.reply_text("Duration must be greater than 0.")
        return

    rest = " ".join(context.args[2:])
    if "|" in rest:
        title, description = rest.split("|", 1)
        title, description = title.strip(), description.strip()
    else:
        title, description = rest.strip(), rest.strip()

    now = int(time.time())
    start_time = now + start_delay
    end_time = start_time + duration
    status = "open" if start_delay == 0 else "scheduled"

    challenge_id = db.create_challenge(title, description, group_chat_id, start_time, end_time, status)
    _schedule_challenge_jobs(context.application.job_queue, challenge_id, start_time, end_time)

    if status == "open":
        await context.bot.send_message(
            chat_id=group_chat_id,
            text=(
                f"🏆 *Challenge #{challenge_id} is now live!*\n\n"
                f"*{esc(title)}*\n{esc(description)}\n\n"
                f"Enter with `/submit {challenge_id} <your entry>`"
            ),
            parse_mode="Markdown",
        )
        await update.message.reply_text(f"Challenge #{challenge_id} created and live now.")
    else:
        start_str = datetime.fromtimestamp(start_time, tz=timezone.utc).strftime("%b %d, %H:%M UTC")
        await update.message.reply_text(
            f"Challenge #{challenge_id} scheduled to open {start_str}, "
            f"runs for {context.args[1]}."
        )


async def announcewinner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /announcewinner <challenge_id> <@username>")
        return
    try:
        challenge_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Challenge ID must be a number.")
        return
    challenge = db.get_challenge(challenge_id)
    if not challenge:
        await update.message.reply_text("That challenge doesn't exist.")
        return
    if challenge["status"] == "scheduled":
        await update.message.reply_text(
            "That challenge hasn't opened yet — wait until it's live before announcing a winner."
        )
        return
    if challenge["winner_id"]:
        await update.message.reply_text("That challenge already has a winner.")
        return

    username = context.args[1].lstrip("@")
    winner = db.find_user_by_username(username)
    if not winner:
        await update.message.reply_text(
            f"Can't find @{username} — they need to have messaged the bot at least once (e.g. /start)."
        )
        return
    if not db.has_submission(challenge_id, winner["telegram_id"]):
        force = len(context.args) > 2 and context.args[2].lower() == "force"
        if not force:
            await update.message.reply_text(
                f"@{username} doesn't have a submission on record for challenge #{challenge_id}. "
                f"Check /submissions {challenge_id} to confirm before announcing — this is a "
                "safety check to catch typos in the wrong username.\n\n"
                f"If this is correct anyway, run: /announcewinner {challenge_id} @{username} force"
            )
            return

    emoji, label, tiers = CATEGORIES["weekly_win"]
    points = tiers["gold"]
    db.award_points(winner["telegram_id"], "weekly_win", "gold", points, update.effective_user.id,
                     f"Winner of challenge #{challenge_id}")
    db.close_challenge(challenge_id, winner["telegram_id"])

    weekly_reward = float(db.get_setting("weekly_reward_dtf", "0"))
    if weekly_reward > 0:
        period_label = f"challenge-{challenge_id}"
        db.upsert_payout(
            winner["telegram_id"], winner["wallet_address"], weekly_reward,
            "weekly", period_label, note=f"Winner of challenge #{challenge_id}",
        )

    if challenge["chat_id"]:
        reward_note = f" and *{weekly_reward} $DTF*" if weekly_reward > 0 else ""
        await context.bot.send_message(
            chat_id=challenge["chat_id"],
            text=(
                f"🏆 Congrats to @{esc(username)} — winner of *Challenge #{challenge_id}: "
                f"{esc(challenge['title'])}*! +{points} Stakeholder Points{reward_note} 🎉"
            ),
            parse_mode="Markdown",
        )
    wallet_note = "" if winner["wallet_address"] else " ⚠️ they're not registered — no wallet on file yet."
    await update.message.reply_text(f"Winner announced and points/payout recorded.{wallet_note}")


async def award(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /award @username <category> <tier> [note]\n\n"
            f"Categories:\n{category_help_text()}\n\nTiers: bronze / silver / gold"
        )
        return

    username = context.args[0].lstrip("@")
    target = db.find_user_by_username(username)
    if not target:
        await update.message.reply_text(
            f"Can't find @{username} — they need to have messaged the bot at least once."
        )
        return

    category = context.args[1].lower()
    if category not in CATEGORIES:
        await update.message.reply_text(f"Unknown category `{category}`. Options:\n{category_help_text()}")
        return

    tier = context.args[2].lower()
    if tier not in TIERS:
        await update.message.reply_text("Tier must be bronze, silver, or gold.")
        return

    emoji, label, tiers = CATEGORIES[category]
    points = tiers[tier]
    note = " ".join(context.args[3:]) if len(context.args) > 3 else ""

    cap = DAILY_CATEGORY_CAP.get(category)
    if cap is not None:
        already = db.get_points_today_for_category(target["telegram_id"], category)
        if already + points > cap:
            await update.message.reply_text(
                f"24-hour cap reached for {label} ({already}/{cap} pts in the last 24h). Award skipped."
            )
            return

    db.award_points(target["telegram_id"], category, tier, points, update.effective_user.id, note)
    note_txt = f" — _{esc(note)}_" if note else ""
    await update.message.reply_text(
        f"{emoji} Awarded *{points} pts* ({tier}) to @{esc(username)} for {label}{note_txt}",
        parse_mode="Markdown",
    )


async def deduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /deduct @username <points> <reason>")
        return
    username = context.args[0].lstrip("@")
    target = db.find_user_by_username(username)
    if not target:
        await update.message.reply_text(f"Can't find @{username}.")
        return
    try:
        points = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Second argument must be a number of points to deduct.")
        return
    reason = " ".join(context.args[2:]) or "manual correction"
    db.award_points(target["telegram_id"], "correction", "-", -abs(points), update.effective_user.id, reason)
    await update.message.reply_text(f"Deducted {points} pts from @{username}: {reason}")


async def resetmonth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    label = " ".join(context.args) if context.args else "unlabeled period"
    rows = db.reset_month(label)
    await update.message.reply_text(
        f"Monthly points archived for {len(rows)} members under '{label}' and reset to 0."
    )


async def pastmonths(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reads back what /resetmonth archived — without this, monthly_archive
    is write-only and past standings are unrecoverable without opening the
    database file directly."""
    if not await require_admin_dm(update):
        return
    if not context.args:
        periods = db.list_archived_periods()
        if not periods:
            await update.message.reply_text("No archived months yet — /resetmonth hasn't been run.")
            return
        lines = ["*Archived periods* (use /pastmonths <label> for the standings):\n"]
        for p in periods:
            lines.append(f"— {esc(p['period_label'])}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    period_label = " ".join(context.args)
    rows = db.get_archived_period(period_label)
    if not rows:
        await update.message.reply_text(f"No archive found for '{period_label}'. See /pastmonths for the list.")
        return

    lines = [f"*Final standings — {esc(period_label)}*\n"]
    for i, r in enumerate(rows, start=1):
        who = f"@{esc(r['username'])}" if r["username"] else str(r["telegram_id"])
        lines.append(f"{i}. {who} — {r['points']} pts")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- payout admin commands -------------------------------------------------

async def setpool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if not context.args:
        current = db.get_setting("monthly_pool_dtf", "0")
        await update.message.reply_text(f"Current monthly pool: {current} $DTF\nUsage: /setpool <amount>")
        return
    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    db.set_setting("monthly_pool_dtf", amount)
    await update.message.reply_text(f"Monthly $DTF pool set to {amount}.")


async def setweeklyreward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if not context.args:
        current = db.get_setting("weekly_reward_dtf", "0")
        await update.message.reply_text(f"Current weekly win reward: {current} $DTF\nUsage: /setweeklyreward <amount>")
        return
    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    db.set_setting("weekly_reward_dtf", amount)
    await update.message.reply_text(f"Weekly challenge win reward set to {amount} $DTF per win.")


async def payoutmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if not context.args or context.args[0].lower() not in ("auto", "manual"):
        current = db.get_setting("payout_mode", "manual")
        await update.message.reply_text(f"Current monthly payout mode: {current}\nUsage: /payoutmode <auto|manual>")
        return
    mode = context.args[0].lower()
    db.set_setting("payout_mode", mode)
    await update.message.reply_text(f"Monthly payout mode set to {mode}.")


async def settopn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if not context.args:
        current = db.get_setting("payout_top_n", "10")
        await update.message.reply_text(f"Currently rewarding the top {current} scorers.\nUsage: /settopn <number>")
        return
    try:
        n = int(context.args[0])
        if n <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Must be a positive whole number.")
        return
    db.set_setting("payout_top_n", n)
    await update.message.reply_text(f"Monthly pool will now be split among the top {n} scorers.")


async def generatepayout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /generatepayout <period_label>\ne.g. /generatepayout August-2026")
        return
    period_label = " ".join(context.args)
    mode = db.get_setting("payout_mode", "manual")
    pool = float(db.get_setting("monthly_pool_dtf", "0"))
    top_n = int(db.get_setting("payout_top_n", "10"))
    registered = db.get_registered_users()

    if not registered:
        await update.message.reply_text("No registered users yet.")
        return

    # Only the top N monthly scorers are eligible — this is the ranked
    # cumulative monthly leaderboard (all categories and all challenges
    # feed the same monthly_points total), not a per-challenge list.
    ranked = sorted(
        (u for u in registered if u["monthly_points"] > 0),
        key=lambda r: r["monthly_points"], reverse=True,
    )
    top = ranked[:top_n]
    total_points = sum(u["monthly_points"] for u in top)

    if mode == "auto":
        if pool <= 0:
            await update.message.reply_text("Monthly pool is 0 — set it with /setpool first.")
            return
        if total_points <= 0:
            await update.message.reply_text("No monthly points recorded yet for registered users.")
            return
        db.clear_pending_payouts("monthly", period_label)
        lines = [f"*Auto payout — {esc(period_label)}* ({pool} $DTF pool, top {top_n})\n"]

        # Largest-remainder method: rounding each share independently can
        # drift the total a few fractions of $DTF away from the actual
        # pool amount. Working in integer 0.0001-$DTF units and handing
        # out the leftover units to whoever has the biggest rounding
        # remainder keeps the total exact.
        UNIT = 10000  # 4 decimal places
        pool_units = round(pool * UNIT)
        raw_shares = [(u, u["monthly_points"] / total_points * pool_units) for u in top]
        floor_shares = [(u, int(share)) for u, share in raw_shares]
        remainders = sorted(
            range(len(raw_shares)), key=lambda i: raw_shares[i][1] - floor_shares[i][1], reverse=True
        )
        leftover = pool_units - sum(s for _, s in floor_shares)
        final_units = [s for _, s in floor_shares]
        for i in remainders[:leftover]:
            final_units[i] += 1

        for (u, _), units in zip(raw_shares, final_units):
            amount = units / UNIT
            db.upsert_payout(u["telegram_id"], u["wallet_address"], amount, "monthly", period_label)
            who = format_user(u["username"], u["display_name"])
            lines.append(f"{who} — {u['monthly_points']} pts → {amount} $DTF")
        lines.append("\nReview above, then /finalizepayout monthly " + period_label)
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    else:
        if not top:
            await update.message.reply_text("No monthly points recorded yet for registered users.")
            return
        lines = [f"*Manual payout — {esc(period_label)}* (top {top_n}, no amounts set yet)\n"]
        for u in top:
            who = format_user(u["username"], u["display_name"])
            lines.append(f"{who} — {u['monthly_points']} pts")
        lines.append(
            "\nSet amounts with `/setpayout @username <amount> " + period_label + "`, "
            "then `/finalizepayout monthly " + period_label + "`."
        )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def setpayout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /setpayout @username <amount> <period_label>")
        return
    username = context.args[0].lstrip("@")
    target = db.find_user_by_username(username)
    if not target:
        await update.message.reply_text(f"Can't find @{username}.")
        return
    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    period_label = " ".join(context.args[2:])
    db.upsert_payout(target["telegram_id"], target["wallet_address"], amount, "monthly", period_label)
    wallet_note = "" if target["wallet_address"] else " ⚠️ no wallet on file — they haven't registered."
    await update.message.reply_text(f"Set {amount} $DTF for @{username} ({period_label}).{wallet_note}")


async def payoutlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /payoutlist <weekly|monthly> <period_label>")
        return
    cycle = context.args[0].lower()
    period_label = " ".join(context.args[1:])
    rows = db.get_payouts(cycle, period_label)
    if not rows:
        await update.message.reply_text("No payout entries for that period.")
        return
    lines = [f"*{cycle.title()} payouts — {esc(period_label)}*\n"]
    total = 0.0
    for r in rows:
        u = db.get_user(r["telegram_id"])
        who = format_user(u["username"], u["display_name"]) if u else str(r["telegram_id"])
        wallet = esc(r["wallet_address"]) if r["wallet_address"] else "⚠️ no wallet"
        lines.append(f"{who} — {r['amount_dtf']} $DTF — `{wallet}` — {r['status']}")
        total += r["amount_dtf"]
    lines.append(f"\nTotal: {round(total, 4)} $DTF")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def finalizepayout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_dm(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /finalizepayout <weekly|monthly> <period_label>")
        return
    cycle = context.args[0].lower()
    period_label = " ".join(context.args[1:])
    rows = db.get_payouts(cycle, period_label, status="pending")
    if not rows:
        await update.message.reply_text("No pending payouts for that period.")
        return
    db.finalize_payouts(cycle, period_label)
    lines = [f"*Finalized — {cycle} — {period_label}*\nCopy this into your send tool:\n"]
    for r in rows:
        wallet = r["wallet_address"] or "MISSING_WALLET"
        lines.append(f"`{wallet}`, {r['amount_dtf']}")
    lines.append(
        "\nNote: I calculate this list but don't broadcast on-chain transactions — "
        "send it yourself via your treasury wallet or a multisend tool."
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- command menu setup (hides admin commands from non-admins) ------------

PUBLIC_COMMANDS = [
    BotCommand("start", "Intro to the league"),
    BotCommand("rules", "Point categories and how it works"),
    BotCommand("register", "Register your wallet (DM only)"),
    BotCommand("leaderboard", "See rankings"),
    BotCommand("mypoints", "Your point totals"),
    BotCommand("mystats", "Your point breakdown"),
    BotCommand("listchallenges", "See live/scheduled challenges"),
    BotCommand("status", "Contest status: deadlines, entries, headcount"),
    BotCommand("submit", "Submit an entry to a challenge"),
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("submissions", "Review entries for a challenge"),
    BotCommand("approve", "Mark a submission approved"),
    BotCommand("reject", "Mark a submission rejected"),
    BotCommand("award", "Award tiered points"),
    BotCommand("deduct", "Deduct points"),
    BotCommand("newchallenge", "Create a scheduled challenge"),
    BotCommand("announcewinner", "Announce a challenge winner"),
    BotCommand("setgroup", "Set announcement group (run in group)"),
    BotCommand("postupdate", "Post status digest to group now"),
    BotCommand("setupdateinterval", "How often auto status updates post"),
    BotCommand("setpool", "Set monthly $DTF pool"),
    BotCommand("setweeklyreward", "Set weekly win $DTF reward"),
    BotCommand("payoutmode", "auto or manual monthly split"),
    BotCommand("settopn", "How many top scorers get paid monthly"),
    BotCommand("generatepayout", "Generate payout list"),
    BotCommand("setpayout", "Manually set a payout amount"),
    BotCommand("payoutlist", "View payout entries"),
    BotCommand("finalizepayout", "Lock in payout list"),
    BotCommand("resetmonth", "Archive and reset monthly points"),
    BotCommand("pastmonths", "View archived monthly standings"),
]


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global fallback: without this, an unhandled exception in any
    command just vanishes silently — no message to the user, nothing
    visible unless you happen to be watching the logs at that exact
    moment. This logs every failure AND pings admins in DM so it
    actually gets noticed."""
    logger.error("Unhandled exception while processing an update", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong handling that — an admin's been notified."
            )
        except Exception:
            pass

    error_summary = f"{type(context.error).__name__}: {context.error}"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ Bot error: {esc(error_summary)[:500]}",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def post_init(application: Application):
    await application.bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:
            logger.warning(f"Could not set admin command menu for {admin_id}: {e}")

    # rebuild scheduling for any challenge that's still scheduled/open,
    # so a bot restart never loses timing
    for c in db.get_challenges_by_status("scheduled", "open"):
        _schedule_challenge_jobs(application.job_queue, c["id"], c["start_time"], c["end_time"])

    # start the recurring status digest based on the saved interval
    hours = float(db.get_setting("status_update_hours", "12"))
    _reschedule_status_digest(application.job_queue, hours)


def main():
    db.init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("mypoints", mypoints))
    app.add_handler(CommandHandler("mystats", mystats))
    app.add_handler(CommandHandler("listchallenges", listchallenges))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("submissions", submissions))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))

    app.add_handler(CommandHandler("award", award))
    app.add_handler(CommandHandler("deduct", deduct))
    app.add_handler(CommandHandler("newchallenge", newchallenge))
    app.add_handler(CommandHandler("announcewinner", announcewinner))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("postupdate", postupdate))
    app.add_handler(CommandHandler("setupdateinterval", setupdateinterval))
    app.add_handler(CommandHandler("setpool", setpool))
    app.add_handler(CommandHandler("setweeklyreward", setweeklyreward))
    app.add_handler(CommandHandler("payoutmode", payoutmode))
    app.add_handler(CommandHandler("settopn", settopn))
    app.add_handler(CommandHandler("generatepayout", generatepayout))
    app.add_handler(CommandHandler("setpayout", setpayout))
    app.add_handler(CommandHandler("payoutlist", payoutlist))
    app.add_handler(CommandHandler("finalizepayout", finalizepayout))
    app.add_handler(CommandHandler("resetmonth", resetmonth))
    app.add_handler(CommandHandler("pastmonths", pastmonths))

    logger.info("DTF Stakeholders League bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

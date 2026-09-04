"""
DTF Stakeholders League — configuration.

Fill in BOT_TOKEN and ADMIN_IDS before running.
"""

import os

# --- Core secrets / IDs ---------------------------------------------------

BOT_TOKEN = os.environ.get("DTF_BOT_TOKEN", "8786271988:AAFPDhciaS5OaBlcLxepfpa9gWysWtdpLSM")

# Only these Telegram numeric user IDs can run admin commands.
# Keep this to just yourself — that's what keeps the backend private.
# Get your ID from @userinfobot.
ADMIN_IDS = {7486730015
    # 123456789,  # <- replace with your Telegram numeric ID
}

DATABASE_PATH = os.environ.get("DTF_DB_PATH", "dtf_league.db")

# --- Point categories (tiered) ---------------------------------------------
# key -> (emoji, label, {"bronze": x, "silver": y, "gold": z})
CATEGORIES = {
    "knowledge":  ("🧠", "Knowledge Question",      {"bronze": 5,  "silver": 10, "gold": 15}),
    "analysis":   ("📊", "Treasury/Market Analysis", {"bronze": 10, "silver": 20, "gold": 35}),
    "prediction": ("🎯", "Accurate Prediction",      {"bronze": 10, "silver": 20, "gold": 35}),
    "suggestion": ("💡", "Ecosystem Suggestion",     {"bronze": 10, "silver": 15, "gold": 25}),
    "research":   ("🔎", "Research Challenge",       {"bronze": 15, "silver": 25, "gold": 40}),
    "helping":    ("🤝", "Helping New Members",       {"bronze": 5,  "silver": 8,  "gold": 12}),
    "discussion": ("🗣️", "Meaningful Discussion",     {"bronze": 3,  "silver": 6,  "gold": 10}),
    "content":    ("📢", "Quality DTF Content",      {"bronze": 10, "silver": 20, "gold": 35}),
    # weekly_win is flat (not tiered) — the reward is winning the challenge itself
    "weekly_win": ("🏆", "Weekly Challenge Win",     {"bronze": 50, "silver": 50, "gold": 50}),
}

TIERS = ("bronze", "silver", "gold")

# Anti-farming: max points a single user can earn per category per day
# (set to None to disable a cap for that category)
DAILY_CATEGORY_CAP = {
    "discussion": 20,
    "helping": 20,
    "knowledge": 30,
}

# Leaderboard display size
LEADERBOARD_SIZE = 10

# Default dynamic settings (overridable at runtime via admin commands;
# stored in the DB `settings` table so they persist and can change without
# redeploying). These are just the fallback values used the first time
# the bot runs.
DEFAULT_SETTINGS = {
    "monthly_pool_dtf": "0",
    "weekly_reward_dtf": "0",
    "payout_mode": "manual",   # "auto" | "manual" — used by /generatepayout monthly
    "payout_top_n": "10",      # only this many top monthly scorers share the pool
    "group_chat_id": "",       # set via /setgroup, run once inside the group
    "status_update_hours": "12",  # how often the bot auto-posts a contest status digest (0 = off)
}

# DTF Stakeholders League Bot

A Telegram bot running a monthly $DTF Stakeholder Points contest for
[dtf.finance](https://dtf.finance) (Robinhood Chain) — points for knowledge,
analysis, predictions, content, and community help, plus independently
scheduled weekly challenges with their own instant $DTF reward.

## Setup

1. **Create the bot** via [@BotFather](https://t.me/BotFather), copy the token.
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure `config.py`**:
   - Set `DTF_BOT_TOKEN` env var (or paste directly into `config.py`)
   - Add **only your own** Telegram numeric ID to `ADMIN_IDS` — get it from
     [@userinfobot](https://t.me/userinfobot). This is what keeps every
     admin command private to you.
4. **Run it**:
   ```bash
   python main.py
   ```
5. Add the bot to your group as an admin.
6. **One-time setup**: inside the group, run `/setgroup` — this tells the
   bot where to post public announcements (challenge openings/closings,
   winners). You only ever need to do this once.

## Keeping the backend private

Every admin command (`/award`, `/newchallenge`, `/setpool`,
`/generatepayout`, etc.) **only works when you DM the bot privately** — if
you run one in the group by mistake, the bot tells you to move to DM
instead of executing it. Nobody in the group ever sees you issuing
commands. The command menu itself is also scoped: regular members only
ever see the public commands in Telegram's `/` autocomplete; the admin
command list is set up to appear only in your own private chat with the
bot.

The only exception is `/setgroup`, which has to be run inside the group
once so the bot can capture its chat ID.

## Registration (required for payouts)

Members run, **in DM only** (the bot declines it in the group so personal
details never sit in the public chat log):
```
/register <wallet address> <twitter handle or link>
```
Telegram username/ID is captured automatically the moment they message
the bot — this just adds wallet and Twitter, so an individual's effort
across the group, submissions, and Twitter promotion can all be traced
back to one applicant. Anyone can still earn points without registering,
but they're excluded from payout generation and flagged with ⚠️ on the
leaderboard until they register.

## Automated contest status updates

The bot posts a status digest to the group on its own — live challenges
with time remaining and entry counts, upcoming scheduled challenges, and
total registered participants:

```
/setupdateinterval <hours>   # e.g. 12 — how often it auto-posts (0 = off)
/postupdate                  # post one right now, on demand
```

Members can also pull the same digest anytime with `/status`, and see
just the challenge list with `/listchallenges`.

## Tiered points

Every category has Bronze / Silver / Gold point values — you choose the
tier when awarding based on effort/quality:

```
/award @username analysis gold great treasury breakdown this week
/award @username knowledge bronze
```

See `config.py` → `CATEGORIES` for the full tier table, or `/rules` in
the bot.

## Challenges — independent and schedulable

Each challenge is its own entity with its own start/end time, so several
can be scheduled or live at once — they don't block each other.

```
/newchallenge <start_delay> <duration> <title> | <description>
```
- `start_delay`: `now`, `2h`, `1d`, `1w`
- `duration`: `12h`, `3d`, `1w`

Example:
```
/newchallenge 1d 3d Treasury Thread | Write a thread explaining DTF's fee-split mechanism
```
Opens automatically in 1 day, stays open 3 days, then auto-closes — the
bot posts the "live now" and "submissions closed" announcements to the
group on its own. Scheduling survives bot restarts (it's rebuilt from
the database on startup, not held only in memory).

Members see everything open/upcoming with `/listchallenges` and enter
with `/submit <challenge_id> <entry>` (requires registration).

Pick a winner:
```
/announcewinner <challenge_id> <@username>
```
This awards Gold-tier weekly-win points, closes the challenge, posts the
public shoutout, and — if you've set a weekly reward amount — queues an
instant weekly $DTF payout for that winner.

## Payouts

Two separate reward cadences, both configurable:

**Weekly** — a fixed $DTF amount per challenge win. Set it with:
```
/setweeklyreward <amount>
```
This pays out automatically the moment you `/announcewinner`.

**Monthly** — the bulk pool, split only among your **top N** scorers on
the cumulative monthly leaderboard (default top 10 — every category and
every challenge feeds this one ranked total, there's no separate
per-challenge leaderboard). Adjust how many get paid:
```
/settopn <number>
```
Set the pool size any time:
```
/setpool <amount>
```
Choose how it gets divided — your call, switchable anytime:
```
/payoutmode auto      # bot proportionally splits the pool by monthly points
/payoutmode manual     # you set custom amounts per person
```

Generate the payout list:
```
/generatepayout August-2026
```
- In **auto** mode this immediately computes each registered member's
  proportional share and stores it as pending.
- In **manual** mode it shows standings only; set amounts yourself with
  `/setpayout @username <amount> August-2026` for each person.

Review anytime with `/payoutlist monthly August-2026`, then lock it in:
```
/finalizepayout monthly August-2026
```
This returns a clean `wallet, amount` list for every recipient.

**Important**: the bot calculates the payout list but does **not**
broadcast on-chain transactions. Sending $DTF requires either your
treasury wallet or a multisend tool — copy the finalized list in. Putting
a treasury private key inside a Telegram bot is a real security decision
that deserves its own deliberate build, not something to bolt on here.

At the end of the month, reset the leaderboard (all-time totals are
untouched):
```
/resetmonth August-2026
```

## User commands

- `/start`, `/rules` — intro and full category/tier breakdown
- `/register <wallet>` — DM only
- `/leaderboard` (`/leaderboard all` for all-time)
- `/mypoints`, `/mystats`
- `/listchallenges`, `/submit <id> <entry>`

## Database

SQLite (`dtf_league.db`, auto-created). Set `DTF_DB_PATH` to change the
location. All access goes through `db.py`, so swapping to Postgres later
doesn't touch bot logic.

## Updating the bot after it's live

Two different kinds of "update," both fully supported:

**Settings you'll change often** (pool size, top N, weekly reward, payout
mode, update interval) are already live-adjustable with no restart —
they're stored in the database and take effect on your very next command.

**Code changes** (new point categories, new commands, behavior tweaks)
require editing the `.py` files and restarting the bot (`python main.py`).
Nothing is lost on restart: all points, users, wallets, challenges, and
payout history live in `dtf_league.db`, not in memory — even scheduled
challenge open/close times and the status digest schedule are rebuilt
from the database automatically on startup. So you can safely stop the
bot, make a change, and bring it back up mid-contest.

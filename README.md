# disc-bot

A small Discord bot for a few private servers, built with [discord.py](https://discordpy.readthedocs.io/).

Current features:

- Slash commands (synced instantly to the servers in `GUILD_IDS`)
- Phrase reactions — replies when messages contain configured trigger words
- Per-user, per-server counters and an interaction log, stored in SQLite

## Requirements

- Python 3.12
- A Discord application with a bot token, with **Message Content Intent**
  enabled (Developer Portal → your app → Bot → Privileged Gateway Intents) —
  required for phrase reactions and `c!` prefix commands.

## Setup

1. Create a virtual environment and install dependencies:

   ```sh
   # macOS / Linux
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

   # Windows
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your bot token and the guild
   (server) IDs you want slash commands synced to.

3. Run the bot:

   ```sh
   python -m bot
   ```

## Project layout

```
bot/
├── __main__.py   # entry point: python -m bot
├── main.py       # LeBot class, extension loading, slash-command sync
├── config.py     # loads .env into a Config object
├── db.py         # SQLite connection, migration runner, query helpers
├── migrations/   # numbered, forward-only SQL migration files
└── cogs/         # one module per feature area
```

Each feature lives in its own cog module under `bot/cogs/`. To add one,
create the module and register it in `INITIAL_EXTENSIONS` in `bot/main.py`.

## Database

State lives in a SQLite file (default `data/bot.db`, override with `DB_PATH`).

Migrations are numbered SQL files in `bot/migrations/` and are forward-only.
On startup the bot compares the database's `PRAGMA user_version` against the
migration files and applies anything newer, in order. Before applying to an
existing database it snapshots the file to `<db>.pre-vNNN.bak` — rolling back
a migration means stopping the bot and restoring that file.

To change the schema: add the next numbered file, e.g.
`bot/migrations/NNN_<description>.sql` (never edit an already-applied file),
and restart the bot.

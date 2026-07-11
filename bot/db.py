import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def connect(db_path: str | Path) -> aiosqlite.Connection:
    """Open the database, apply any pending migrations, and return the connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA foreign_keys = ON")
    await _apply_migrations(db, path)
    return db


async def _apply_migrations(db: aiosqlite.Connection, path: Path) -> None:
    """Run any numbered migration files above the database's current version.

    Migrations are forward-only. Rolling back means restoring the .bak
    snapshot taken here before the first pending migration ran.
    """
    async with db.execute("PRAGMA user_version") as cursor:
        current = (await cursor.fetchone())[0]

    migrations = sorted(
        (int(file.stem.split("_", 1)[0]), file)
        for file in MIGRATIONS_DIR.glob("*.sql")
    )
    pending = [(version, file) for version, file in migrations if version > current]
    if not pending:
        return

    if current > 0:
        backup = path.with_name(f"{path.name}.pre-v{pending[0][0]:03d}.bak")
        backup.unlink(missing_ok=True)
        await db.execute("VACUUM INTO ?", (str(backup),))
        log.info("Database backed up to %s", backup)

    for version, file in pending:
        log.info("Applying migration %s", file.name)
        await db.executescript(file.read_text())
        await db.execute(f"PRAGMA user_version = {version}")
    await db.commit()


async def increment_counter(
    db: aiosqlite.Connection,
    guild_id: int,
    user_id: int,
    name: str,
    by: int = 1,
) -> int:
    """Add to a per-user counter and return its new value."""
    async with db.execute(
        """
        INSERT INTO counters (guild_id, user_id, name, value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (guild_id, user_id, name)
        DO UPDATE SET value = value + excluded.value
        RETURNING value
        """,
        (guild_id, user_id, name, by),
    ) as cursor:
        value = (await cursor.fetchone())[0]
    await db.commit()
    return value


async def get_counter(
    db: aiosqlite.Connection,
    guild_id: int,
    user_id: int,
    name: str,
) -> int:
    async with db.execute(
        """SELECT value 
           FROM counters 
           WHERE guild_id = ? 
             AND user_id = ? 
             AND name = ?""",
        (guild_id, user_id, name),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else 0


async def get_reaction_blocked_channels(db: aiosqlite.Connection) -> set[int]:
    """All blocked channel IDs across every guild, for fast in-memory checks."""
    async with db.execute("SELECT channel_id FROM reaction_blocked_channels") as cursor:
        return {row[0] for row in await cursor.fetchall()}


async def block_reaction_channel(
        db: aiosqlite.Connection,
        guild_id: int,
        channel_id: int,
) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO reaction_blocked_channels (channel_id, guild_id) VALUES (?, ?)",
        (channel_id, guild_id),
    )
    await db.commit()


async def unblock_reaction_channel(db: aiosqlite.Connection, channel_id: int) -> None:
    await db.execute(
        "DELETE FROM reaction_blocked_channels WHERE channel_id = ?", (channel_id,)
    )
    await db.commit()


async def list_reaction_blocked_channels(
        db: aiosqlite.Connection,
        guild_id: int,
) -> list[int]:
    async with db.execute(
        "SELECT channel_id FROM reaction_blocked_channels WHERE guild_id = ?",
        (guild_id,)
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def log_event(
    db: aiosqlite.Connection,
    guild_id: int,
    user_id: int,
    event_type: str,
    detail: str | None = None,
) -> None:
    """Append a row to the interaction log."""
    await db.execute(
        "INSERT INTO events (guild_id, user_id, event_type, detail) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, event_type, detail),
    )
    await db.commit()

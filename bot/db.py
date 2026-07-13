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


async def add_lobby_game(
    db: aiosqlite.Connection,
    guild_id: int,
    name: str,
    party_size: int,
    emoji: str | None = None,
) -> int | None:
    """Register a game for lobbies. Returns its id, or None if the name is taken."""
    async with db.execute(
        """
        INSERT INTO lobby_games (guild_id, name, party_size, emoji)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (guild_id, name) DO NOTHING
        RETURNING id
        """,
        (guild_id, name, party_size, emoji),
    ) as cursor:
        row = await cursor.fetchone()
    await db.commit()
    return row[0] if row else None


async def update_lobby_game(
    db: aiosqlite.Connection,
    game_id: int,
    party_size: int | None = None,
    emoji: str | None = None,
) -> None:
    await db.execute(
        """
        UPDATE lobby_games
        SET party_size = coalesce(?, party_size),
            emoji = coalesce(?, emoji)
        WHERE id = ?
        """,
        (party_size, emoji, game_id),
    )
    await db.commit()


async def remove_lobby_game(db: aiosqlite.Connection, guild_id: int, name: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM lobby_games WHERE guild_id = ? AND name = ?", (guild_id, name)
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_lobby_game(
    db: aiosqlite.Connection,
    guild_id: int,
    name: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM lobby_games WHERE guild_id = ? AND name = ?", (guild_id, name)
    ) as cursor:
        return await cursor.fetchone()


async def list_lobby_games(
    db: aiosqlite.Connection,
    guild_id: int,
) -> list[aiosqlite.Row]:
    async with db.execute(
        """
        SELECT g.*, count(p.user_id) AS ping_list_size
        FROM lobby_games g
        LEFT JOIN game_ping_list p ON p.game_id = g.id
        WHERE g.guild_id = ?
        GROUP BY g.id
        ORDER BY g.name
        """,
        (guild_id,),
    ) as cursor:
        return list(await cursor.fetchall())


async def add_to_ping_list(db: aiosqlite.Connection, game_id: int, user_id: int) -> bool:
    cursor = await db.execute(
        "INSERT OR IGNORE INTO game_ping_list (game_id, user_id) VALUES (?, ?)",
        (game_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def remove_from_ping_list(
    db: aiosqlite.Connection,
    game_id: int,
    user_id: int,
) -> bool:
    cursor = await db.execute(
        "DELETE FROM game_ping_list WHERE game_id = ? AND user_id = ?",
        (game_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_ping_list(db: aiosqlite.Connection, game_id: int) -> list[int]:
    async with db.execute(
        "SELECT user_id FROM game_ping_list WHERE game_id = ?", (game_id,)
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


_LOBBY_WITH_GAME = """
    SELECT l.*, g.name, g.party_size, g.emoji
    FROM lobbies l
    JOIN lobby_games g ON g.id = l.game_id
"""


async def create_lobby(
    db: aiosqlite.Connection,
    game_id: int,
    guild_id: int,
    channel_id: int,
    message_id: int,
    created_by: int,
) -> int:
    async with db.execute(
        """
        INSERT INTO lobbies (game_id, guild_id, channel_id, message_id, created_by)
        VALUES (?, ?, ?, ?, ?)
        RETURNING id
        """,
        (game_id, guild_id, channel_id, message_id, created_by),
    ) as cursor:
        lobby_id = (await cursor.fetchone())[0]
    await db.commit()
    return lobby_id


async def move_lobby(
    db: aiosqlite.Connection,
    lobby_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    """Point a lobby at a new message (re-open moves and auto-bumps)."""
    await db.execute(
        "UPDATE lobbies SET channel_id = ?, message_id = ? WHERE id = ?",
        (channel_id, message_id, lobby_id),
    )
    await db.commit()


async def get_lobby_by_message(
    db: aiosqlite.Connection,
    message_id: int,
) -> aiosqlite.Row | None:
    async with db.execute(
        _LOBBY_WITH_GAME + "WHERE l.message_id = ?", (message_id,)
    ) as cursor:
        return await cursor.fetchone()


async def get_lobby_for_game(
    db: aiosqlite.Connection,
    game_id: int,
) -> aiosqlite.Row | None:
    async with db.execute(
        _LOBBY_WITH_GAME + "WHERE l.game_id = ?", (game_id,)
    ) as cursor:
        return await cursor.fetchone()


async def delete_lobby(db: aiosqlite.Connection, lobby_id: int) -> None:
    await db.execute("DELETE FROM lobbies WHERE id = ?", (lobby_id,))
    await db.commit()


async def delete_lobby_by_message(db: aiosqlite.Connection, message_id: int) -> bool:
    cursor = await db.execute(
        "DELETE FROM lobbies WHERE message_id = ?", (message_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


async def add_lobby_member(
    db: aiosqlite.Connection,
    lobby_id: int,
    user_id: int,
    added_by: int,
) -> bool:
    """Add a user to a lobby. Returns False if they were already in it."""
    cursor = await db.execute(
        "INSERT OR IGNORE INTO lobby_members (lobby_id, user_id, added_by) VALUES (?, ?, ?)",
        (lobby_id, user_id, added_by),
    )
    await db.commit()
    return cursor.rowcount > 0


async def remove_lobby_member(
    db: aiosqlite.Connection,
    lobby_id: int,
    user_id: int,
) -> bool:
    cursor = await db.execute(
        "DELETE FROM lobby_members WHERE lobby_id = ? AND user_id = ?",
        (lobby_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def clear_lobby_members(db: aiosqlite.Connection, lobby_id: int) -> None:
    """Wipe a lobby's members and re-arm its full-lobby announcement."""
    await db.execute("DELETE FROM lobby_members WHERE lobby_id = ?", (lobby_id,))
    await db.execute("UPDATE lobbies SET announced = 0 WHERE id = ?", (lobby_id,))
    await db.commit()


async def list_lobby_members(db: aiosqlite.Connection, lobby_id: int) -> list[int]:
    async with db.execute(
        "SELECT user_id FROM lobby_members WHERE lobby_id = ? ORDER BY joined_at, user_id",
        (lobby_id,),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def mark_lobby_announced(db: aiosqlite.Connection, lobby_id: int) -> bool:
    """Flip the announced flag. Returns False if it was already set."""
    cursor = await db.execute(
        "UPDATE lobbies SET announced = 1 WHERE id = ? AND announced = 0", (lobby_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


async def touch_lobby_ping(db: aiosqlite.Connection, lobby_id: int) -> None:
    await db.execute(
        "UPDATE lobbies SET last_ping_at = datetime('now') WHERE id = ?", (lobby_id,)
    )
    await db.commit()

import asyncio
import sqlite3
import time
import weakref
from collections.abc import Awaitable, Callable
from datetime import timedelta

import aiosqlite
import discord

from bot import db
from bot.cogs.lobby.avatars import AvatarStrips
from bot.cogs.lobby.options import NoOpenLobby
from bot.cogs.lobby.views import LobbyLayout, game_label
from bot.main import LeBot

PING_COOLDOWN = timedelta(seconds=30)
MAX_PING_MENTIONS = 50


class LobbyService:
    """Owns lobby state and the message lifecycle: membership changes, the
    debounced render loop, and lobby creation/move/close."""

    def __init__(self, bot: LeBot):
        self.bot = bot
        self.avatars = AvatarStrips(bot)
        # Per-lobby render debounce: one render runs at a time and bursts
        # coalesce into it, so concurrent joins/kicks can't both replace the
        # lobby message and strand a duplicate. Weak values let a lobby's
        # lock vanish once no render is holding it.
        self._render_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # lobby_id -> whether the pending redraw should bump the message.
        self._render_pending: dict[int, bool] = {}
        # guild_id -> emoji -> game row, so the message listener doesn't
        # query on every message. Invalidated whenever /game changes.
        self._emoji_games: dict[int, dict[str, aiosqlite.Row]] = {}

    @property
    def db(self) -> aiosqlite.Connection:
        return self.bot.db

    async def lobby_for(
        self, interaction: discord.Interaction
    ) -> aiosqlite.Row | None:
        """The lobby a component interaction belongs to, or None (handled)."""
        lobby = await db.get_lobby_by_message(self.db, interaction.message.id)
        if lobby is None:
            await interaction.response.send_message(
                "This lobby is no longer active.", ephemeral=True
            )
            # Self-heal: a lobby-looking message that isn't in the DB is a
            # stranded duplicate - delete it rather than leave a corpse.
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                # Can't delete (e.g. missing permission): at least defuse it.
                stale = discord.ui.LayoutView(timeout=None)
                stale.add_item(
                    discord.ui.TextDisplay("*This lobby is no longer active.*")
                )
                try:
                    await interaction.message.edit(view=stale)
                except discord.HTTPException:
                    pass
        return lobby

    async def lobby_for_game(self, game: aiosqlite.Row) -> aiosqlite.Row:
        """The game's open lobby, or raise NoOpenLobby for the shared handler."""
        lobby = await db.get_lobby_for_game(self.db, game["id"])
        if lobby is None:
            raise NoOpenLobby(game)
        return lobby

    async def game_by_emoji(
        self, guild_id: int, emoji: str
    ) -> aiosqlite.Row | None:
        games = self._emoji_games.get(guild_id)
        if games is None:
            rows = await db.list_lobby_games(self.db, guild_id)
            games = {row["emoji"]: row for row in rows if row["emoji"]}
            self._emoji_games[guild_id] = games
        return games.get(emoji)

    def invalidate_games(self, guild_id: int) -> None:
        """Drop the guild's emoji cache after a /game add/edit/remove."""
        self._emoji_games.pop(guild_id, None)

    async def get_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel

    async def lobby_view(
        self,
        game: aiosqlite.Row,
        member_ids: list[int],
        history: list[aiosqlite.Row] | None = None,
    ) -> tuple[LobbyLayout, discord.File | None]:
        strip = await self.avatars.strip_for(member_ids)
        view = LobbyLayout(
            self, game, member_ids, has_strip=strip is not None, history=history
        )
        return view, strip

    # -- membership ---------------------------------------------------------

    async def add_member(
        self, lobby: aiosqlite.Row, target_id: int, actor_id: int
    ) -> bool:
        """Add target to the lobby; record history and the audit log on change."""
        if not await db.add_lobby_member(self.db, lobby["id"], target_id, actor_id):
            return False
        if actor_id == target_id:
            await db.add_lobby_history(self.db, lobby["id"], actor_id, "join")
        else:
            await db.add_lobby_history(
                self.db, lobby["id"], actor_id, "add", target_id
            )
        await db.log_event(
            self.db, lobby["guild_id"], actor_id,
            "lobby_member_add", f"{lobby['name']}:{target_id}",
        )
        return True

    async def remove_member(
        self, lobby: aiosqlite.Row, target_id: int, actor_id: int
    ) -> bool:
        """Remove target from the lobby; record history and the audit log on change."""
        if not await db.remove_lobby_member(self.db, lobby["id"], target_id):
            return False
        if actor_id == target_id:
            await db.add_lobby_history(self.db, lobby["id"], actor_id, "leave")
        else:
            await db.add_lobby_history(
                self.db, lobby["id"], actor_id, "remove", target_id
            )
        await db.log_event(
            self.db, lobby["guild_id"], actor_id,
            "lobby_member_remove", f"{lobby['name']}:{target_id}",
        )
        return True

    async def clear_members(self, lobby: aiosqlite.Row, actor_id: int) -> None:
        await db.clear_lobby_members(self.db, lobby["id"])
        await db.add_lobby_history(self.db, lobby["id"], actor_id, "clear")
        await db.log_event(
            self.db, lobby["guild_id"], actor_id, "lobby_clear", lobby["name"]
        )

    async def after_member_change(self, lobby: aiosqlite.Row) -> None:
        """Announce a filled party (once), then redraw/bump the lobby."""
        members = await db.list_lobby_members(self.db, lobby["id"])
        if len(members) >= lobby["party_size"]:
            if await db.mark_lobby_announced(self.db, lobby["id"]):
                channel = await self.get_channel(lobby["channel_id"])
                if channel is not None:
                    mentions = " ".join(f"<@{uid}>" for uid in members)
                    try:
                        await channel.send(
                            f"The {game_label(lobby)} lobby is full: {mentions}",
                            allowed_mentions=discord.AllowedMentions(users=True),
                        )
                    except discord.HTTPException:
                        pass
                await db.log_event(
                    self.db, lobby["guild_id"], lobby["created_by"],
                    "lobby_full", lobby["name"],
                )
        await self.render(lobby, bump=True)

    # -- pings --------------------------------------------------------------

    def ping_cooldown_remaining(self, lobby: aiosqlite.Row) -> int | None:
        """Whole seconds left on the lobby's ping cooldown, or None if free."""
        if lobby["last_ping_ts"] is None:
            return None
        remaining = (
            PING_COOLDOWN.total_seconds() - (time.time() - lobby["last_ping_ts"])
        )
        if remaining <= 0:
            return None
        return max(1, int(remaining))

    async def ping_targets(self, lobby: aiosqlite.Row) -> list[int]:
        """The game's ping list, minus anyone already in the lobby."""
        members = await db.list_lobby_members(self.db, lobby["id"])
        return [
            uid
            for uid in await db.get_ping_list(self.db, lobby["game_id"])
            if uid not in members
        ]

    async def send_pings(
        self,
        channel: discord.abc.Messageable,
        lobby: aiosqlite.Row,
        actor_id: int,
        targets: list[int],
    ) -> None:
        """Mention the targets in chunks, then stamp the cooldown and log."""
        mentions = [f"<@{uid}>" for uid in targets]
        while mentions:
            chunk, mentions = (
                mentions[:MAX_PING_MENTIONS], mentions[MAX_PING_MENTIONS:]
            )
            await channel.send(
                " ".join(chunk),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        await db.touch_lobby_ping(self.db, lobby["id"])
        await db.log_event(
            self.db, lobby["guild_id"], actor_id, "lobby_ping", lobby["name"]
        )

    # -- rendering ----------------------------------------------------------

    def _render_lock(self, lobby_id: int) -> asyncio.Lock:
        return self._render_locks.setdefault(lobby_id, asyncio.Lock())

    async def render(self, lobby: aiosqlite.Row, *, bump: bool) -> None:
        """Request a redraw of a lobby message, debounced per lobby.

        Only one redraw runs at a time; requests that arrive while one is in
        flight coalesce into a single follow-up pass. Without this, two
        concurrent member changes could both replace the lobby message and
        strand a dead duplicate.
        """
        lobby_id = lobby["id"]
        self._render_pending[lobby_id] = (
            self._render_pending.get(lobby_id, False) or bump
        )
        lock = self._render_lock(lobby_id)
        if lock.locked():
            return  # the in-flight render's loop will pick this up
        async with lock:
            while lobby_id in self._render_pending:
                bump_now = self._render_pending.pop(lobby_id)
                await self._render_once(lobby_id, bump=bump_now)

    async def _render_once(self, lobby_id: int, *, bump: bool) -> None:
        # Re-fetch inside the lock: a previous pass (or /lobby open) may
        # have moved the lobby to a new message since the caller's row.
        lobby = await db.get_lobby(self.db, lobby_id)
        if lobby is None:
            return
        channel = await self.get_channel(lobby["channel_id"])
        if channel is None:
            return
        members = await db.list_lobby_members(self.db, lobby_id)
        history = await db.list_lobby_history(self.db, lobby_id)
        view, strip = await self.lobby_view(lobby, members, history)

        replace = bump and channel.last_message_id != lobby["message_id"]
        if not replace:
            try:
                await channel.get_partial_message(lobby["message_id"]).edit(
                    view=view,
                    attachments=[strip] if strip else [],
                )
                return
            except discord.NotFound:
                await db.delete_lobby(self.db, lobby_id)
                return
            except discord.HTTPException:
                # e.g. a lobby message from before the layout rework can't
                # be edited into the new format - replace it instead.
                view, strip = await self.lobby_view(lobby, members, history)

        try:
            message = await channel.send(
                view=view, **({"files": [strip]} if strip else {})
            )
        except discord.HTTPException:
            return
        # Repoint the DB before deleting, so the raw-delete listener
        # doesn't mistake our own bump for the lobby being removed.
        await db.move_lobby(self.db, lobby_id, channel.id, message.id)
        try:
            await channel.get_partial_message(lobby["message_id"]).delete()
        except discord.HTTPException:
            pass

    # -- lobby lifecycle ----------------------------------------------------

    async def claim_lobby_message(
        self,
        game: aiosqlite.Row,
        guild_id: int,
        message: discord.Message,
        created_by: int,
    ) -> tuple[aiosqlite.Row | None, bool]:
        """Record a just-sent message as the game's lobby.

        On losing the unique-lobby race (someone opened this game's lobby at
        the same moment), delete our message instead of stranding a duplicate
        and return the winner's row with created=False; the caller decides
        whether the winner needs a redraw.
        """
        try:
            lobby_id = await db.create_lobby(
                self.db, game["id"], guild_id, message.channel.id, message.id,
                created_by,
            )
        except sqlite3.IntegrityError:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return await db.get_lobby_for_game(self.db, game["id"]), False
        await db.log_event(self.db, guild_id, created_by, "lobby_open", game["name"])
        return await db.get_lobby(self.db, lobby_id), True

    async def open_lobby(
        self,
        channel: discord.abc.Messageable,
        guild_id: int,
        game: aiosqlite.Row,
        created_by: int,
    ) -> aiosqlite.Row | None:
        """Post a fresh lobby message for the game and record it."""
        view, strip = await self.lobby_view(game, [])
        try:
            posted = await channel.send(
                view=view, **({"files": [strip]} if strip else {})
            )
        except discord.HTTPException:
            return None
        lobby, _ = await self.claim_lobby_message(game, guild_id, posted, created_by)
        return lobby

    async def move_lobby_message(
        self,
        lobby: aiosqlite.Row,
        send: Callable[[LobbyLayout, discord.File | None], Awaitable[discord.Message]],
    ) -> None:
        """Move the lobby to a newly sent message, members intact.

        Holds the render lock across the send so a concurrent member-change
        redraw can't also replace the message and strand a duplicate.
        """
        async with self._render_lock(lobby["id"]):
            lobby = await db.get_lobby(self.db, lobby["id"]) or lobby
            members = await db.list_lobby_members(self.db, lobby["id"])
            history = await db.list_lobby_history(self.db, lobby["id"])
            view, strip = await self.lobby_view(lobby, members, history)
            message = await send(view, strip)
            old_channel_id, old_message_id = (
                lobby["channel_id"], lobby["message_id"]
            )
            await db.move_lobby(self.db, lobby["id"], message.channel.id, message.id)
            old_channel = await self.get_channel(old_channel_id)
            if old_channel is not None:
                try:
                    await old_channel.get_partial_message(old_message_id).delete()
                except discord.HTTPException:
                    pass

    async def close_lobby(self, lobby: aiosqlite.Row) -> None:
        """Delete the lobby and swap its message for a closed card."""
        await db.delete_lobby(self.db, lobby["id"])
        channel = await self.get_channel(lobby["channel_id"])
        if channel is None:
            return
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"## {game_label(lobby)} lobby\n*Closed.*"),
                accent_colour=discord.Colour.dark_grey(),
            )
        )
        try:
            await channel.get_partial_message(lobby["message_id"]).edit(
                view=closed,
                attachments=[],
            )
        except discord.HTTPException:
            pass

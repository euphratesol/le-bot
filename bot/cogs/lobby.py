import asyncio
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

from bot import db
from bot.main import LeBot

log = logging.getLogger(__name__)

PING_COOLDOWN = timedelta(seconds=30)
MAX_PING_MENTIONS = 50
AVATAR_SIZE = 48
AVATAR_PAD = 8
MAX_AVATARS = 10
AVATAR_CACHE_LIMIT = 100
STRIP_FILENAME = "lobby.png"
MAX_KICK_BUTTONS = 15
LOBBIES = {"valorant": "batsignal"}


def _game_label(row: aiosqlite.Row) -> str:
    return f"{row['emoji']} {row['name']}" if row["emoji"] else row["name"]


def _compose_avatar_strip(avatars: list[bytes]) -> bytes:
    size, pad = AVATAR_SIZE, AVATAR_PAD
    canvas = Image.new(
        "RGBA",
        (pad + len(avatars) * (size + pad), size + 2 * pad),
        (0, 0, 0, 0),
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    for slot, blob in enumerate(avatars):
        head = Image.open(BytesIO(blob)).convert("RGBA").resize((size, size))
        canvas.paste(head, (pad + slot * (size + pad), pad), mask)
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def _parse_emoji(raw: str) -> str | None:
    emoji = discord.PartialEmoji.from_str(raw.strip())
    if emoji.is_custom_emoji():
        return str(emoji)
    # Unicode emoji: from_str puts arbitrary text in .name, so filter out
    # anything that reads as plain words rather than emoji characters.
    name = emoji.name.strip()
    if name and len(name) <= 8 and not any(ch.isalnum() and ch.isascii() for ch in name):
        return name
    return None


class KickButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lobby:kick:(?P<user_id>[0-9]+)",
):
    def __init__(self, user_id: int, label: str = "x"):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=label,
                custom_id=f"lobby:kick:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: "re.Match[str]",
    ) -> "KickButton":
        return cls(int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Lobby | None" = interaction.client.get_cog("Lobby")
        if cog is None:
            return
        lobby = await cog.lobby_for(interaction)
        if lobby is None:
            return
        removed = await db.remove_lobby_member(
            cog.bot.db, lobby["id"], self.user_id
        )
        if not removed:
            await interaction.response.send_message(
                f"<@{self.user_id}> isn't in this lobby anymore.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none()
            )
            return
        if self.user_id == interaction.user.id:
            notice = f"{interaction.user.mention} left the {_game_label(lobby)} lobby."
        else:
            notice = (
                f"{interaction.user.mention} removed <@{self.user_id}> from the "
                f"{_game_label(lobby)} lobby."
            )
        await interaction.response.send_message(
            notice, allowed_mentions=discord.AllowedMentions.none()
        )
        await db.log_event(
            cog.bot.db, lobby["guild_id"], interaction.user.id,
            "lobby_member_remove", f"{lobby['name']}:{self.user_id}",
        )
        await cog.after_member_change(lobby)


class AddPlayerSelect(
    discord.ui.DynamicItem[discord.ui.UserSelect],
    template=r"lobby:add_select:[0-9]+",
):
    def __init__(self):
        super().__init__(
            discord.ui.UserSelect(
                placeholder="Add a player...",
                custom_id=f"lobby:add_select:{time.time_ns()}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.UserSelect,
        match: "re.Match[str]",
    ) -> "AddPlayerSelect":
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Lobby | None" = interaction.client.get_cog("Lobby")
        if cog is None:
            return
        lobby = await cog.lobby_for(interaction)
        if lobby is None:
            return
        cog.stage_add(lobby["id"], interaction.user.id, self.item.values[0].id)
        # Leave the pick visible in the dropdown as feedback; Add commits it.
        await interaction.response.defer()

class LobbyControls(discord.ui.ActionRow):
    """The Add/Join/Leave/Ping/Clear buttons on every lobby message."""

    def __init__(self, cog: "Lobby"):
        super().__init__()
        self.cog = cog

    @discord.ui.button(
        label="Add", style=discord.ButtonStyle.primary, custom_id="lobby:add_confirm"
    )
    async def add(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.cog.lobby_for(interaction)
        if lobby is None:
            return
        staged = self.cog.take_staged_add(lobby["id"], interaction.user.id)
        if staged is None:
            await interaction.response.send_message(
                "Pick someone in the dropdown first.", ephemeral=True
            )
            return
        added = await db.add_lobby_member(
            self.cog.bot.db, lobby["id"], staged, interaction.user.id
        )
        # Respond by editing the message with a rebuilt view - the fresh
        # picker nonce is what actually clears the dropdown.
        members = await db.list_lobby_members(self.cog.bot.db, lobby["id"])
        view, strip = await self.cog._lobby_view(lobby, members)
        try:
            await interaction.response.edit_message(
                view=view, attachments=[strip] if strip else []
            )
        except discord.HTTPException:
            pass
        if not added:
            await interaction.followup.send(
                f"<@{staged}> is already in the lobby.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.followup.send(
            f"{interaction.user.mention} added <@{staged}> to the "
            f"{_game_label(lobby)} lobby.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await db.log_event(
            self.cog.bot.db, lobby["guild_id"], interaction.user.id,
            "lobby_member_add", f"{lobby['name']}:{staged}",
        )
        await self.cog.after_member_change(lobby)

    @discord.ui.button(
        label="Join", style=discord.ButtonStyle.success, custom_id="lobby:join"
    )
    async def join(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.cog.lobby_for(interaction)
        if lobby is None:
            return
        added = await db.add_lobby_member(
            self.cog.bot.db, lobby["id"], interaction.user.id, interaction.user.id
        )
        if not added:
            await interaction.response.send_message(
                "You're already in this lobby.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"{interaction.user.mention} joined the {_game_label(lobby)} lobby.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await db.log_event(
            self.cog.bot.db, lobby["guild_id"], interaction.user.id,
            "lobby_member_add", f"{lobby['name']}:{interaction.user.id}",
        )
        await self.cog.after_member_change(lobby)

    @discord.ui.button(
        label="Leave", style=discord.ButtonStyle.secondary, custom_id="lobby:leave"
    )
    async def leave(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.cog.lobby_for(interaction)
        if lobby is None:
            return
        removed = await db.remove_lobby_member(
            self.cog.bot.db, lobby["id"], interaction.user.id
        )
        if not removed:
            await interaction.response.send_message(
                "You're not in this lobby.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"{interaction.user.mention} left the {_game_label(lobby)} lobby.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await db.log_event(
            self.cog.bot.db, lobby["guild_id"], interaction.user.id,
            "lobby_member_remove", f"{lobby['name']}:{interaction.user.id}",
        )
        await self.cog.after_member_change(lobby)

    @discord.ui.button(
        label="Ping",
        style=discord.ButtonStyle.primary,
        custom_id="lobby:ping",
    )
    async def ping(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.cog.lobby_for(interaction)
        if lobby is None:
            return

        if lobby["last_ping_at"]:
            last = datetime.strptime(
                lobby["last_ping_at"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            remaining = PING_COOLDOWN - (datetime.now(timezone.utc) - last)
            if remaining > timedelta(0):
                await interaction.response.send_message(
                    f"Ping cooldown, try again in {max(1, int(remaining.total_seconds()))}s.",
                    ephemeral=True,
                )
                return

        members = await db.list_lobby_members(self.cog.bot.db, lobby["id"])
        targets = [
            uid
            for uid in await db.get_ping_list(self.cog.bot.db, lobby["game_id"])
            if uid not in members
        ]
        if not targets:
            await interaction.response.send_message(
                f"No one to ping for {_game_label(lobby)} - an admin can add "
                "people with `/game pinglist add`.",
                ephemeral=True,
            )
            return

        mentions = [f"<@{uid}>" for uid in targets]
        first, rest = mentions[:MAX_PING_MENTIONS], mentions[MAX_PING_MENTIONS:]
        await interaction.response.send_message(
            f"{' '.join(first)}"
        )
        while rest:
            chunk, rest = rest[:MAX_PING_MENTIONS], rest[MAX_PING_MENTIONS:]
            await interaction.channel.send(" ".join(chunk))
        await db.touch_lobby_ping(self.cog.bot.db, lobby["id"])
        await db.log_event(
            self.cog.bot.db, lobby["guild_id"], interaction.user.id,
            "lobby_ping", lobby["name"],
        )

    @discord.ui.button(
        label="Clear", style=discord.ButtonStyle.danger, custom_id="lobby:clear"
    )
    async def clear(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.cog.lobby_for(interaction)
        if lobby is None:
            return
        await db.clear_lobby_members(self.cog.bot.db, lobby["id"])
        await interaction.response.send_message(
            f"{interaction.user.mention} cleared the {_game_label(lobby)} lobby.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.cog.render(lobby, bump=True)
        await db.log_event(
            self.cog.bot.db, lobby["guild_id"], interaction.user.id,
            "lobby_clear", lobby["name"],
        )


class LobbyLayout(discord.ui.LayoutView):
    """The whole lobby message: member rows with inline x buttons, the
    avatar strip, and the add/control rows.

    Built fresh for each render. A bare LobbyLayout(cog) is registered as
    the persistent template so the static custom_ids survive restarts
    (KickButton dispatch is covered separately by add_dynamic_items).
    """

    # Discord caps a message at 40 components, nested ones included. The
    # fixed parts (container, heading, footer, gallery, three action rows,
    # select, Add button, four control buttons) cost 13; a member row with
    # an inline x costs 3 (section + text + button), a plain text row 1.
    ROW_BUDGET = 28

    def __init__(
        self,
        cog: "Lobby",
        game: aiosqlite.Row | None = None,
        member_ids: list[int] | None = None,
        has_strip: bool = False,
    ):
        super().__init__(timeout=None)
        if game is not None:
            self.add_item(self._container(cog, game, member_ids or [], has_strip))
            self.add_item(discord.ui.ActionRow(AddPlayerSelect()))
        self.add_item(LobbyControls(cog))

    @staticmethod
    def _container(
        cog: "Lobby",
        game: aiosqlite.Row,
        members: list[int],
        has_strip: bool,
    ) -> discord.ui.Container:
        party_size = game["party_size"]
        slots = min(max(party_size, len(members)), LobbyLayout.ROW_BUDGET)
        max_inline = min(
            MAX_KICK_BUTTONS, max(0, (LobbyLayout.ROW_BUDGET - slots) // 2)
        )

        rows: list[discord.ui.Item] = [
            discord.ui.TextDisplay(f"## {_game_label(game)} lobby")
        ]
        for slot in range(slots):
            if slot < len(members):
                line = f"{slot + 1}. <@{members[slot]}>"
                if slot < max_inline:
                    rows.append(
                        discord.ui.Section(
                            discord.ui.TextDisplay(line),
                            accessory=KickButton(members[slot]),
                        )
                    )
                else:
                    rows.append(discord.ui.TextDisplay(line))
            else:
                rows.append(discord.ui.TextDisplay(f"{slot + 1}."))
        if has_strip:
            rows.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(f"attachment://{STRIP_FILENAME}")
                )
            )
        rows.append(discord.ui.TextDisplay(f"-# {len(members)}/{party_size} players"))

        full = len(members) >= party_size
        return discord.ui.Container(
            *rows,
            accent_colour=discord.Colour.green() if full else discord.Colour.blurple(),
        )


class Lobby(commands.Cog):
    """Game lobby organiser: gather players, then ping a per-game list."""

    lobby_group = app_commands.Group(
        name="lobby",
        description="Organise a game lobby.",
        guild_only=True,
    )
    game_group = app_commands.Group(
        name="game",
        description="Configure games available for lobbies.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )
    pinglist_group = app_commands.Group(
        name="pinglist",
        description="Manage who gets pinged for a game's lobbies.",
        parent=game_group,
    )

    def __init__(self, bot: LeBot):
        self.bot = bot
        self.view = LobbyLayout(self)
        self._avatar_cache: dict[str, bytes] = {}
        # (lobby_id, picker's user_id) -> user_id staged in the dropdown.
        self._staged_adds: dict[tuple[int, int], int] = {}
        # Per-lobby render debounce: one render runs at a time and bursts
        # coalesce into it, so concurrent joins/kicks can't both replace the
        # lobby message and strand a duplicate.
        self._render_locks: dict[int, asyncio.Lock] = {}
        self._render_dirty: set[int] = set()
        self._render_bump: dict[int, bool] = {}

    async def cog_load(self) -> None:
        # One persistent template handles the static components on every
        # lobby message; DynamicItem registration covers the x buttons and
        # the nonce'd add dropdown.
        self.bot.add_view(self.view)
        self.bot.add_dynamic_items(KickButton, AddPlayerSelect)

    def stage_add(self, lobby_id: int, picker_id: int, user_id: int) -> None:
        if len(self._staged_adds) > 500:
            self._staged_adds.pop(next(iter(self._staged_adds)))
        self._staged_adds[(lobby_id, picker_id)] = user_id

    def take_staged_add(self, lobby_id: int, picker_id: int) -> int | None:
        return self._staged_adds.pop((lobby_id, picker_id), None)

    async def game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        games = await db.list_lobby_games(self.bot.db, interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=game["name"], value=game["name"])
            for game in games
            if current in game["name"].lower()
        ][:25]

    async def _resolve_game(
        self, interaction: discord.Interaction, name: str
    ) -> aiosqlite.Row | None:
        game = await db.get_lobby_game(self.bot.db, interaction.guild_id, name)
        if game is None:
            await interaction.response.send_message(
                f"There's no game called **{name}** here - an admin can add it "
                "with `/game add`.",
                ephemeral=True,
            )
        return game

    async def lobby_for(
        self, interaction: discord.Interaction
    ) -> aiosqlite.Row | None:
        """The lobby a component interaction belongs to, or None (handled)."""
        lobby = await db.get_lobby_by_message(self.bot.db, interaction.message.id)
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

    async def _get_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel

    async def _resolve_user(self, user_id: int) -> discord.User | None:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return None
        return user

    async def _avatar_bytes(self, user_id: int) -> bytes | None:
        user = await self._resolve_user(user_id)
        if user is None:
            return None
        asset = user.display_avatar.replace(size=128, format="png")
        cached = self._avatar_cache.get(asset.url)
        if cached is not None:
            return cached
        try:
            data = await asset.read()
        except discord.HTTPException:
            return None
        if len(self._avatar_cache) > AVATAR_CACHE_LIMIT:
            self._avatar_cache.pop(next(iter(self._avatar_cache)))
        self._avatar_cache[asset.url] = data
        return data

    async def _avatar_strip(self, member_ids: list[int]) -> discord.File | None:
        avatars = []
        for user_id in member_ids[:MAX_AVATARS]:
            blob = await self._avatar_bytes(user_id)
            if blob is not None:
                avatars.append(blob)
        if not avatars:
            return None
        return discord.File(
            BytesIO(_compose_avatar_strip(avatars)), filename=STRIP_FILENAME
        )

    async def _lobby_view(
        self,
        game: aiosqlite.Row,
        member_ids: list[int],
    ) -> tuple[LobbyLayout, discord.File | None]:
        strip = await self._avatar_strip(member_ids)
        view = LobbyLayout(self, game, member_ids, has_strip=strip is not None)
        return view, strip

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
        self._render_dirty.add(lobby_id)
        self._render_bump[lobby_id] = self._render_bump.get(lobby_id, False) or bump
        lock = self._render_lock(lobby_id)
        if lock.locked():
            return  # the in-flight render's loop will pick this up
        async with lock:
            while lobby_id in self._render_dirty:
                self._render_dirty.discard(lobby_id)
                bump_now = self._render_bump.pop(lobby_id, False)
                await self._render_once(lobby_id, bump=bump_now)

    async def _render_once(self, lobby_id: int, *, bump: bool) -> None:
        # Re-fetch inside the lock: a previous pass (or /lobby open) may
        # have moved the lobby to a new message since the caller's row.
        lobby = await db.get_lobby(self.bot.db, lobby_id)
        if lobby is None:
            return
        channel = await self._get_channel(lobby["channel_id"])
        if channel is None:
            return
        members = await db.list_lobby_members(self.bot.db, lobby_id)
        view, strip = await self._lobby_view(lobby, members)

        replace = bump and channel.last_message_id != lobby["message_id"]
        if not replace:
            try:
                await channel.get_partial_message(lobby["message_id"]).edit(
                    view=view,
                    attachments=[strip] if strip else [],
                )
                return
            except discord.NotFound:
                await db.delete_lobby(self.bot.db, lobby_id)
                return
            except discord.HTTPException:
                # e.g. a lobby message from before the layout rework can't
                # be edited into the new format - replace it instead.
                view, strip = await self._lobby_view(lobby, members)

        try:
            message = await channel.send(
                view=view, **({"files": [strip]} if strip else {})
            )
        except discord.HTTPException:
            return
        # Repoint the DB before deleting, so the raw-delete listener
        # doesn't mistake our own bump for the lobby being removed.
        await db.move_lobby(self.bot.db, lobby_id, channel.id, message.id)
        try:
            await channel.get_partial_message(lobby["message_id"]).delete()
        except discord.HTTPException:
            pass

    async def after_member_change(self, lobby: aiosqlite.Row) -> None:
        """Announce a filled party (once), then redraw/bump the lobby."""
        members = await db.list_lobby_members(self.bot.db, lobby["id"])
        if len(members) >= lobby["party_size"]:
            if await db.mark_lobby_announced(self.bot.db, lobby["id"]):
                channel = await self._get_channel(lobby["channel_id"])
                if channel is not None:
                    mentions = " ".join(f"<@{uid}>" for uid in members)
                    try:
                        await channel.send(
                            f"The {_game_label(lobby)} lobby is full: {mentions}"
                        )
                    except discord.HTTPException:
                        pass
                await db.log_event(
                    self.bot.db, lobby["guild_id"], lobby["created_by"],
                    "lobby_full", lobby["name"],
                )
        await self.render(lobby, bump=True)

    @lobby_group.command(description="Open the lobby for a game (or bring it here).")
    @app_commands.autocomplete(game=game_autocomplete)
    async def open(self, interaction: discord.Interaction, game: str) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return

        lobby = await db.get_lobby_for_game(self.bot.db, game_row["id"])
        if lobby is not None:
            # Move the existing lobby here, members intact. Hold the render
            # lock so a concurrent member-change redraw can't also replace
            # the message and strand a duplicate.
            async with self._render_lock(lobby["id"]):
                lobby = await db.get_lobby(self.bot.db, lobby["id"]) or lobby
                members = await db.list_lobby_members(self.bot.db, lobby["id"])
                view, strip = await self._lobby_view(lobby, members)
                await interaction.response.send_message(
                    view=view, **({"files": [strip]} if strip else {})
                )
                message = await interaction.original_response()
                old_channel_id, old_message_id = (
                    lobby["channel_id"], lobby["message_id"]
                )
                await db.move_lobby(
                    self.bot.db, lobby["id"], message.channel.id, message.id
                )
                old_channel = await self._get_channel(old_channel_id)
                if old_channel is not None:
                    try:
                        await old_channel.get_partial_message(old_message_id).delete()
                    except discord.HTTPException:
                        pass
        else:
            view, strip = await self._lobby_view(game_row, [])
            await interaction.response.send_message(
                view=view, **({"files": [strip]} if strip else {})
            )
            message = await interaction.original_response()
            try:
                await db.create_lobby(
                    self.bot.db,
                    game_row["id"],
                    interaction.guild_id,
                    message.channel.id,
                    message.id,
                    interaction.user.id,
                )
            except sqlite3.IntegrityError:
                # Someone opened this game's lobby at the same moment and
                # won the unique-lobby race - drop our message and surface
                # theirs instead of stranding a duplicate.
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                existing = await db.get_lobby_for_game(self.bot.db, game_row["id"])
                if existing is not None:
                    await self.render(existing, bump=True)
                return
        await db.log_event(
            self.bot.db, interaction.guild_id, interaction.user.id,
            "lobby_open", game_row["name"],
        )

    @lobby_group.command(name="remove", description="Remove someone from a game's lobby.")
    @app_commands.autocomplete(game=game_autocomplete)
    async def lobby_remove(
        self, interaction: discord.Interaction, user: discord.Member, game: str
    ) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return
        lobby = await db.get_lobby_for_game(self.bot.db, game_row["id"])
        if lobby is None:
            await interaction.response.send_message(
                f"There's no open {_game_label(game_row)} lobby.", ephemeral=True
            )
            return
        removed = await db.remove_lobby_member(self.bot.db, lobby["id"], user.id)
        if not removed:
            await interaction.response.send_message(
                f"{user.mention} isn't in the {_game_label(lobby)} lobby.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            f"{interaction.user.mention} removed {user.mention} from the "
            f"{_game_label(lobby)} lobby.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await db.log_event(
            self.bot.db, lobby["guild_id"], interaction.user.id,
            "lobby_member_remove", f"{lobby['name']}:{user.id}",
        )
        await self.render(lobby, bump=True)

    @lobby_group.command(description="Close a game's lobby.")
    @app_commands.autocomplete(game=game_autocomplete)
    async def close(self, interaction: discord.Interaction, game: str) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return
        lobby = await db.get_lobby_for_game(self.bot.db, game_row["id"])
        if lobby is None:
            await interaction.response.send_message(
                f"There's no open {_game_label(game_row)} lobby.", ephemeral=True
            )
            return
        await db.delete_lobby(self.bot.db, lobby["id"])
        channel = await self._get_channel(lobby["channel_id"])
        if channel is not None:
            closed = discord.ui.LayoutView(timeout=None)
            closed.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(f"## {_game_label(lobby)} lobby\n*Closed.*"),
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
        await interaction.response.send_message(
            f"{interaction.user.mention} closed the {_game_label(lobby)} lobby.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @game_group.command(description="Add a game that lobbies can be opened for.")
    @app_commands.describe(
        name="Name of the game.",
        party_size="How many players make a full lobby.",
        emoji="Optional emoji shown with the game.",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        name: str,
        party_size: app_commands.Range[int, 2, 25],
        emoji: str | None = None,
    ) -> None:
        parsed = None
        if emoji is not None:
            parsed = _parse_emoji(emoji)
            if parsed is None:
                await interaction.response.send_message(
                    f"`{emoji}` doesn't look like an emoji.", ephemeral=True
                )
                return
        name = name.strip()
        game_id = await db.add_lobby_game(
            self.bot.db, interaction.guild_id, name, party_size, parsed
        )
        if game_id is None:
            await interaction.response.send_message(
                f"**{name}** is already set up.", ephemeral=True
            )
            return
        label = f"{parsed} {name}" if parsed else name
        await interaction.response.send_message(
            f"Added **{label}** (party of {party_size}). Open a lobby with "
            f"`/lobby open`.",
            ephemeral=True,
        )

    @game_group.command(description="Change a game's party size or emoji.")
    @app_commands.autocomplete(game=game_autocomplete)
    async def edit(
        self,
        interaction: discord.Interaction,
        game: str,
        party_size: app_commands.Range[int, 2, 25] | None = None,
        emoji: str | None = None,
    ) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return
        parsed = None
        if emoji is not None:
            parsed = _parse_emoji(emoji)
            if parsed is None:
                await interaction.response.send_message(
                    f"`{emoji}` doesn't look like an emoji.", ephemeral=True
                )
                return
        if party_size is None and parsed is None:
            await interaction.response.send_message(
                "Nothing to change - pass a party size and/or an emoji.",
                ephemeral=True,
            )
            return
        await db.update_lobby_game(self.bot.db, game_row["id"], party_size, parsed)
        await interaction.response.send_message(
            f"Updated **{game_row['name']}**.", ephemeral=True
        )
        lobby = await db.get_lobby_for_game(self.bot.db, game_row["id"])
        if lobby is not None:
            await self.render(lobby, bump=False)

    @game_group.command(
        name="remove", description="Remove a game, its ping list, and any open lobby."
    )
    @app_commands.autocomplete(game=game_autocomplete)
    async def game_remove(self, interaction: discord.Interaction, game: str) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return
        lobby = await db.get_lobby_for_game(self.bot.db, game_row["id"])
        await db.remove_lobby_game(self.bot.db, interaction.guild_id, game_row["name"])
        if lobby is not None:
            channel = await self._get_channel(lobby["channel_id"])
            if channel is not None:
                try:
                    await channel.get_partial_message(lobby["message_id"]).delete()
                except discord.HTTPException:
                    pass
        await interaction.response.send_message(
            f"Removed **{game_row['name']}**.", ephemeral=True
        )

    @game_group.command(name="list", description="List the games set up for lobbies.")
    async def list_games(self, interaction: discord.Interaction) -> None:
        games = await db.list_lobby_games(self.bot.db, interaction.guild_id)
        if not games:
            message = "No games set up yet."
        else:
            message = "\n".join(
                f"- **{_game_label(game)}** - party of {game['party_size']}, "
                f"{game['ping_list_size']} on the ping list"
                for game in games
            )
        await interaction.response.send_message(message, ephemeral=True)

    @pinglist_group.command(name="add", description="Add someone to a game's ping list.")
    @app_commands.autocomplete(game=game_autocomplete)
    async def pinglist_add(
        self, interaction: discord.Interaction, user: discord.Member, game: str
    ) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return
        added = await db.add_to_ping_list(self.bot.db, game_row["id"], user.id)
        message = (
            f"{user.mention} will be pinged for {_game_label(game_row)} lobbies."
            if added
            else f"{user.mention} is already on the {_game_label(game_row)} ping list."
        )
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @pinglist_group.command(
        name="remove", description="Take someone off a game's ping list."
    )
    @app_commands.autocomplete(game=game_autocomplete)
    async def pinglist_remove(
        self, interaction: discord.Interaction, user: discord.Member, game: str
    ) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return
        removed = await db.remove_from_ping_list(self.bot.db, game_row["id"], user.id)
        message = (
            f"{user.mention} won't be pinged for {_game_label(game_row)} lobbies anymore."
            if removed
            else f"{user.mention} isn't on the {_game_label(game_row)} ping list."
        )
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @pinglist_group.command(name="show", description="See a game's ping list.")
    @app_commands.autocomplete(game=game_autocomplete)
    async def pinglist_show(self, interaction: discord.Interaction, game: str) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return
        user_ids = await db.get_ping_list(self.bot.db, game_row["id"])
        if not user_ids:
            message = f"The {_game_label(game_row)} ping list is empty."
        else:
            mentions = ", ".join(f"<@{uid}>" for uid in user_ids)
            message = f"Pinged for {_game_label(game_row)} lobbies: {mentions}"
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        # If someone manually deletes a lobby message, drop the lobby with it.
        await db.delete_lobby_by_message(self.bot.db, payload.message_id)

    async def _start_lobby(
        self,
        channel: discord.abc.Messageable,
        guild_id: int,
        game: aiosqlite.Row,
        created_by: int,
    ) -> aiosqlite.Row | None:
        view, strip = await self._lobby_view(game, [])
        try:
            posted = await channel.send(
                view=view, **({"files": [strip]} if strip else {})
            )
        except discord.HTTPException:
            return None
        try:
            lobby_id = await db.create_lobby(
                self.bot.db, game["id"], guild_id, channel.id, posted.id, created_by
            )
        except sqlite3.IntegrityError:
            try:
                await posted.delete()
            except discord.HTTPException:
                pass
            return await db.get_lobby_for_game(self.bot.db, game["id"])
        await db.log_event(
            self.bot.db, guild_id, created_by, "lobby_open", game["name"]
        )
        return await db.get_lobby(self.bot.db, lobby_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        emoji = discord.PartialEmoji.from_str(message.content.strip())
        if not emoji.is_custom_emoji():
            return
        game_name = next(
            (name for name, summon in LOBBIES.items() if summon == emoji.name), None
        )
        if game_name is None:
            return
        games = await db.list_lobby_games(self.bot.db, message.guild.id)
        game = next(
            (g for g in games if g["name"].lower() == game_name.lower()), None
        )
        if game is None:
            return
        lobby = await db.get_lobby_for_game(self.bot.db, game["id"])
        if lobby is None:
            lobby = await self._start_lobby(
                message.channel, message.guild.id, game, message.author.id
            )
            if lobby is None:
                return
        added = await db.add_lobby_member(
            self.bot.db, lobby["id"], message.author.id, message.author.id
        )
        if not added:
            return
        await db.log_event(
            self.bot.db, lobby["guild_id"], message.author.id,
            "lobby_member_add", f"{lobby['name']}:{message.author.id}",
        )
        await self.after_member_change(lobby)


async def setup(bot: LeBot) -> None:
    await bot.add_cog(Lobby(bot))

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from bot import db
from bot.main import LeBot

log = logging.getLogger(__name__)

PING_COOLDOWN = timedelta(seconds=30)
MAX_PING_MENTIONS = 50
EMPTY_SLOT = "—"


def _game_label(row: aiosqlite.Row) -> str:
    """Display name for a game/lobby row, with its emoji if one is set."""
    return f"{row['emoji']} {row['name']}" if row["emoji"] else row["name"]


def _parse_emoji(raw: str) -> str | None:
    """Normalise an emoji input, or None if it doesn't look like one."""
    emoji = discord.PartialEmoji.from_str(raw.strip())
    if emoji.is_custom_emoji():
        return str(emoji)
    # Unicode emoji: from_str puts arbitrary text in .name, so filter out
    # anything that reads as plain words rather than emoji characters.
    name = emoji.name.strip()
    if name and len(name) <= 8 and not any(ch.isalnum() and ch.isascii() for ch in name):
        return name
    return None


class LobbyView(discord.ui.View):
    """Persistent controls attached to every lobby message.

    Static custom_ids + timeout=None let one instance serve all lobbies
    across restarts; callbacks find their lobby by the message they're on.
    """

    def __init__(self, cog: "Lobby"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="➕ Add a player…",
        custom_id="lobby:add_select",
        row=0,
    )
    async def add_player(
        self,
        interaction: discord.Interaction,
        select: discord.ui.UserSelect,
    ) -> None:
        lobby = await self.cog.lobby_for(interaction)
        if lobby is None:
            return
        user = select.values[0]
        if user.bot:
            await interaction.response.send_message(
                "Bots can't join a lobby.", ephemeral=True
            )
            return
        added = await db.add_lobby_member(
            self.cog.bot.db, lobby["id"], user.id, interaction.user.id
        )
        if not added:
            await interaction.response.send_message(
                f"{user.mention} is already in the lobby.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        await self.cog.after_member_change(lobby)

    @discord.ui.button(
        label="Join", style=discord.ButtonStyle.success, custom_id="lobby:join", row=1
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
        await interaction.response.defer()
        await self.cog.after_member_change(lobby)

    @discord.ui.button(
        label="Leave", style=discord.ButtonStyle.secondary, custom_id="lobby:leave", row=1
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
        await interaction.response.defer()
        await self.cog.after_member_change(lobby)

    @discord.ui.button(
        label="Ping", style=discord.ButtonStyle.primary, custom_id="lobby:ping", row=1
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
                    f"The lobby was pinged recently — try again in "
                    f"{max(1, int(remaining.total_seconds()))}s.",
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
                f"No one to ping for {_game_label(lobby)} — an admin can add "
                "people with `/game pinglist add`.",
                ephemeral=True,
            )
            return

        mentions = [f"<@{uid}>" for uid in targets]
        first, rest = mentions[:MAX_PING_MENTIONS], mentions[MAX_PING_MENTIONS:]
        await interaction.response.send_message(
            f"{' '.join(first)} — a {_game_label(lobby)} lobby is looking for players!"
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
        label="Clear", style=discord.ButtonStyle.danger, custom_id="lobby:clear", row=1
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
        self.view = LobbyView(self)

    async def cog_load(self) -> None:
        # One persistent view handles the buttons on every lobby message.
        self.bot.add_view(self.view)

    # --- shared helpers -------------------------------------------------

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
                f"There's no game called **{name}** here — an admin can add it "
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
            try:
                await interaction.message.edit(view=None)
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

    @staticmethod
    def _build_embed(game: aiosqlite.Row, member_ids: list[int]) -> discord.Embed:
        party_size = game["party_size"]
        lines = [f"## {_game_label(game)} lobby"]
        for slot in range(max(party_size, len(member_ids))):
            member = f"<@{member_ids[slot]}>" if slot < len(member_ids) else EMPTY_SLOT
            lines.append(f"{slot + 1}. {member}")
        full = len(member_ids) >= party_size
        embed = discord.Embed(
            description="\n".join(lines),
            colour=discord.Colour.green() if full else discord.Colour.blurple(),
        )
        embed.set_footer(text=f"{len(member_ids)}/{party_size} players")
        return embed

    async def render(self, lobby: aiosqlite.Row, *, bump: bool) -> None:
        """Redraw a lobby message; on member changes, keep it at the bottom.

        `lobby` may be stale on everything except id/game fields — channel
        and message ids are what they were when the caller fetched the row.
        """
        channel = await self._get_channel(lobby["channel_id"])
        if channel is None:
            return
        members = await db.list_lobby_members(self.bot.db, lobby["id"])
        embed = self._build_embed(lobby, members)

        if bump and channel.last_message_id != lobby["message_id"]:
            try:
                message = await channel.send(embed=embed, view=self.view)
            except discord.HTTPException:
                return
            # Repoint the DB before deleting, so the raw-delete listener
            # doesn't mistake our own bump for the lobby being removed.
            await db.move_lobby(self.bot.db, lobby["id"], channel.id, message.id)
            try:
                await channel.get_partial_message(lobby["message_id"]).delete()
            except discord.HTTPException:
                pass
            return

        try:
            await channel.get_partial_message(lobby["message_id"]).edit(embed=embed)
        except discord.NotFound:
            await db.delete_lobby(self.bot.db, lobby["id"])
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

    # --- /lobby ---------------------------------------------------------

    @lobby_group.command(description="Open the lobby for a game (or bring it here).")
    @app_commands.autocomplete(game=game_autocomplete)
    async def open(self, interaction: discord.Interaction, game: str) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return

        lobby = await db.get_lobby_for_game(self.bot.db, game_row["id"])
        if lobby is not None:
            # Move the existing lobby here, members intact.
            await db.add_lobby_member(
                self.bot.db, lobby["id"], interaction.user.id, interaction.user.id
            )
            members = await db.list_lobby_members(self.bot.db, lobby["id"])
            await interaction.response.send_message(
                embed=self._build_embed(lobby, members), view=self.view
            )
            message = await interaction.original_response()
            old_channel_id, old_message_id = lobby["channel_id"], lobby["message_id"]
            await db.move_lobby(self.bot.db, lobby["id"], message.channel.id, message.id)
            old_channel = await self._get_channel(old_channel_id)
            if old_channel is not None:
                try:
                    await old_channel.get_partial_message(old_message_id).delete()
                except discord.HTTPException:
                    pass
        else:
            await interaction.response.send_message(
                embed=self._build_embed(game_row, [interaction.user.id]),
                view=self.view,
            )
            message = await interaction.original_response()
            lobby_id = await db.create_lobby(
                self.bot.db,
                game_row["id"],
                interaction.guild_id,
                message.channel.id,
                message.id,
                interaction.user.id,
            )
            await db.add_lobby_member(
                self.bot.db, lobby_id, interaction.user.id, interaction.user.id
            )
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
            closed = discord.Embed(
                description=f"## {_game_label(lobby)} lobby\n*Closed.*",
                colour=discord.Colour.dark_grey(),
            )
            try:
                await channel.get_partial_message(lobby["message_id"]).edit(
                    embed=closed, view=None
                )
            except discord.HTTPException:
                pass
        await interaction.response.send_message(
            f"{interaction.user.mention} closed the {_game_label(lobby)} lobby.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- /game ------------------------------------------------------------

    @game_group.command(description="Add a game that lobbies can be opened for.")
    @app_commands.describe(
        name="Name of the game (used in commands).",
        party_size="How many players make a full lobby.",
        emoji="Optional emoji shown with the game (custom server emojis work).",
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
                "Nothing to change — pass a party size and/or an emoji.",
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
            message = "No games set up yet — add one with `/game add`."
        else:
            message = "\n".join(
                f"- **{_game_label(game)}** — party of {game['party_size']}, "
                f"{game['ping_list_size']} on the ping list"
                for game in games
            )
        await interaction.response.send_message(message, ephemeral=True)

    # --- /game pinglist ---------------------------------------------------

    @pinglist_group.command(name="add", description="Add someone to a game's ping list.")
    @app_commands.autocomplete(game=game_autocomplete)
    async def pinglist_add(
        self, interaction: discord.Interaction, user: discord.Member, game: str
    ) -> None:
        game_row = await self._resolve_game(interaction, game)
        if game_row is None:
            return
        if user.bot:
            await interaction.response.send_message(
                "Bots don't need pinging.", ephemeral=True
            )
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

    # --- housekeeping -------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        # If someone manually deletes a lobby message, drop the lobby with it.
        await db.delete_lobby_by_message(self.bot.db, payload.message_id)


async def setup(bot: LeBot) -> None:
    await bot.add_cog(Lobby(bot))

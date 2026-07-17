import discord
from discord import app_commands
from discord.ext import commands

from bot import db
from bot.cogs.lobby.options import EmojiOption, GameOption, on_game_command_error
from bot.cogs.lobby.service import LobbyService
from bot.cogs.lobby.views import game_label
from bot.main import LeBot


class GameAdmin(commands.Cog):
    """Admin configuration: games, their ping lists, and lobby membership."""

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
    gamelobby_group = app_commands.Group(
        name="lobby",
        description="Manage a game's lobby members.",
        parent=game_group,
    )

    def __init__(self, bot: LeBot, service: LobbyService):
        self.bot = bot
        self.service = service

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await on_game_command_error(interaction, error)

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
        emoji: EmojiOption | None = None,
    ) -> None:
        name = name.strip()
        game_id = await db.add_lobby_game(
            self.bot.db, interaction.guild_id, name, party_size, emoji
        )
        if game_id is None:
            await interaction.response.send_message(
                f"**{name}** is already set up.", ephemeral=True
            )
            return
        self.service.invalidate_games(interaction.guild_id)
        await db.log_event(
            self.bot.db, interaction.guild_id, interaction.user.id, "game_add", name
        )
        label = f"{emoji} {name}" if emoji else name
        await interaction.response.send_message(
            f"Added **{label}** (party of {party_size}). Open a lobby with "
            f"`/lobby open`.",
            ephemeral=True,
        )

    @game_group.command(description="Change a game's party size or emoji.")
    async def edit(
        self,
        interaction: discord.Interaction,
        game: GameOption,
        party_size: app_commands.Range[int, 2, 25] | None = None,
        emoji: EmojiOption | None = None,
    ) -> None:
        if party_size is None and emoji is None:
            await interaction.response.send_message(
                "Nothing to change - pass a party size and/or an emoji.",
                ephemeral=True,
            )
            return
        await db.update_lobby_game(self.bot.db, game["id"], party_size, emoji)
        self.service.invalidate_games(interaction.guild_id)
        await db.log_event(
            self.bot.db, interaction.guild_id, interaction.user.id,
            "game_edit", game["name"],
        )
        await interaction.response.send_message(
            f"Updated **{game['name']}**.", ephemeral=True
        )
        lobby = await db.get_lobby_for_game(self.bot.db, game["id"])
        if lobby is not None:
            await self.service.render(lobby, bump=False)

    @game_group.command(
        name="remove", description="Remove a game, its ping list, and any open lobby."
    )
    async def game_remove(
        self, interaction: discord.Interaction, game: GameOption
    ) -> None:
        lobby = await db.get_lobby_for_game(self.bot.db, game["id"])
        await db.remove_lobby_game(self.bot.db, interaction.guild_id, game["name"])
        self.service.invalidate_games(interaction.guild_id)
        await db.log_event(
            self.bot.db, interaction.guild_id, interaction.user.id,
            "game_remove", game["name"],
        )
        if lobby is not None:
            channel = await self.service.get_channel(lobby["channel_id"])
            if channel is not None:
                try:
                    await channel.get_partial_message(lobby["message_id"]).delete()
                except discord.HTTPException:
                    pass
        await interaction.response.send_message(
            f"Removed **{game['name']}**.", ephemeral=True
        )

    @game_group.command(name="list", description="List the games set up for lobbies.")
    async def list_games(self, interaction: discord.Interaction) -> None:
        games = await db.list_lobby_games(self.bot.db, interaction.guild_id)
        if not games:
            message = "No games set up yet."
        else:
            message = "\n".join(
                f"- **{game_label(game)}** - party of {game['party_size']}, "
                f"{game['ping_list_size']} on the ping list"
                for game in games
            )
        await interaction.response.send_message(message, ephemeral=True)

    @gamelobby_group.command(
        name="add", description="Add someone to a game's lobby."
    )
    async def lobby_add(
        self, interaction: discord.Interaction, user: discord.Member, game: GameOption
    ) -> None:
        lobby = await self.service.lobby_for_game(game)
        added = await self.service.add_member(lobby, user.id, interaction.user.id)
        if not added:
            await interaction.response.send_message(
                f"{user.mention} is already in the {game_label(lobby)} lobby.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Added {user.mention} to the {game_label(lobby)} lobby.",
            ephemeral=True,
        )
        await self.service.after_member_change(lobby)

    @gamelobby_group.command(
        name="remove", description="Remove someone from a game's lobby."
    )
    async def lobby_remove(
        self, interaction: discord.Interaction, user: discord.Member, game: GameOption
    ) -> None:
        lobby = await self.service.lobby_for_game(game)
        removed = await self.service.remove_member(lobby, user.id, interaction.user.id)
        if not removed:
            await interaction.response.send_message(
                f"{user.mention} isn't in the {game_label(lobby)} lobby.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Removed {user.mention} from the {game_label(lobby)} lobby.",
            ephemeral=True,
        )
        await self.service.render(lobby, bump=True)

    @pinglist_group.command(name="add", description="Add someone to a game's ping list.")
    async def pinglist_add(
        self, interaction: discord.Interaction, user: discord.Member, game: GameOption
    ) -> None:
        added = await db.add_to_ping_list(self.bot.db, game["id"], user.id)
        if added:
            await db.log_event(
                self.bot.db, interaction.guild_id, interaction.user.id,
                "pinglist_add", f"{game['name']}:{user.id}",
            )
        message = (
            f"{user.mention} will be pinged for {game_label(game)} lobbies."
            if added
            else f"{user.mention} is already on the {game_label(game)} ping list."
        )
        await interaction.response.send_message(
            message, ephemeral=True
        )

    @pinglist_group.command(
        name="remove", description="Take someone off a game's ping list."
    )
    async def pinglist_remove(
        self, interaction: discord.Interaction, user: discord.Member, game: GameOption
    ) -> None:
        removed = await db.remove_from_ping_list(self.bot.db, game["id"], user.id)
        if removed:
            await db.log_event(
                self.bot.db, interaction.guild_id, interaction.user.id,
                "pinglist_remove", f"{game['name']}:{user.id}",
            )
        message = (
            f"{user.mention} won't be pinged for {game_label(game)} lobbies anymore."
            if removed
            else f"{user.mention} isn't on the {game_label(game)} ping list."
        )
        await interaction.response.send_message(
            message, ephemeral=True
        )

    @pinglist_group.command(name="show", description="See a game's ping list.")
    async def pinglist_show(
        self, interaction: discord.Interaction, game: GameOption
    ) -> None:
        user_ids = await db.get_ping_list(self.bot.db, game["id"])
        if not user_ids:
            message = f"The {game_label(game)} ping list is empty."
        else:
            mentions = ", ".join(f"<@{uid}>" for uid in user_ids)
            message = f"Pinged for {game_label(game)} lobbies: {mentions}"
        await interaction.response.send_message(
            message, ephemeral=True
        )

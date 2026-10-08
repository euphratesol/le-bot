import discord
from discord import app_commands
from discord.ext import commands

from bot import db
from bot.cogs.lobby.options import GameOption, on_game_command_error, parse_emoji
from bot.cogs.lobby.service import LobbyService
from bot.cogs.lobby.views import LobbyLayout, game_label
from bot.main import LeBot


class Lobby(commands.Cog):
    """Game lobby organiser: gather players, then ping a per-game list."""

    lobby_group = app_commands.Group(
        name="lobby",
        description="Organise a game lobby.",
        guild_only=True,
    )

    def __init__(self, bot: LeBot, service: LobbyService):
        self.bot = bot
        self.service = service
        self.view = LobbyLayout(service)

    async def cog_load(self) -> None:
        # One persistent template handles the static components on every
        # lobby message.
        self.bot.add_view(self.view)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await on_game_command_error(interaction, error)

    @lobby_group.command(description="Open the lobby for a game (or bring it here).")
    async def open(self, interaction: discord.Interaction, game: GameOption) -> None:
        # Moving a lobby can wait on an in-flight render and download avatars,
        # which can miss Discord's 3s reply deadline - acknowledge first.
        await interaction.response.defer(ephemeral=True)
        lobby = await db.get_lobby_for_game(self.bot.db, game["id"])
        if lobby is None:
            lobby = await self.service.open_lobby(
                interaction.channel, interaction.guild_id, game, interaction.user.id
            )
            posted = lobby is not None
        else:
            # Move the existing lobby here, members intact.
            posted = await self.service.move_lobby_message(lobby, interaction.channel)
            if posted:
                await db.log_event(
                    self.bot.db, interaction.guild_id, interaction.user.id,
                    "lobby_move", game["name"],
                )
        await interaction.followup.send(
            f"The {game_label(game)} lobby is up."
            if posted
            else "I couldn't post the lobby in this channel.",
            ephemeral=True,
        )

    @lobby_group.command(description="Close a game's lobby.")
    async def close(self, interaction: discord.Interaction, game: GameOption) -> None:
        lobby = await self.service.lobby_for_game(game)
        await self.service.close_lobby(lobby)
        await interaction.response.send_message(
            f"{interaction.user.mention} closed the {game_label(lobby)} lobby."
        )
        await db.log_event(
            self.bot.db, interaction.guild_id, interaction.user.id,
            "lobby_close", game["name"],
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        # If someone manually deletes a lobby message, drop the lobby with it.
        await db.delete_lobby_by_message(self.bot.db, payload.message_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        emoji = parse_emoji(message.content)
        if emoji is None:
            return
        game = await self.service.game_by_emoji(message.guild.id, emoji)
        if game is None:
            return
        lobby = await db.get_lobby_for_game(self.bot.db, game["id"])
        if lobby is None:
            lobby = await self.service.open_lobby(
                message.channel, message.guild.id, game, message.author.id
            )
            if lobby is None:
                return
        added = await self.service.add_member(
            lobby, message.author.id, message.author.id
        )
        if not added:
            return
        await self.service.after_member_change(lobby)

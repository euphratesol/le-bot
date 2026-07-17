"""Slash-command option plumbing shared by the lobby cogs.

Transformers turn raw option input into validated values (with
autocomplete where it fits) and raise AppCommandErrors on bad input;
``on_game_command_error`` is the shared ``cog_app_command_error`` body
that answers those errors ephemerally.
"""

import logging

import aiosqlite
import discord
from discord import app_commands

from bot import db
from bot.cogs.lobby.views import game_label

log = logging.getLogger(__name__)


def parse_emoji(raw: str) -> str | None:
    emoji = discord.PartialEmoji.from_str(raw.strip())
    if emoji.is_custom_emoji():
        return str(emoji)
    # Unicode emoji: from_str puts arbitrary text in .name, so filter out
    # anything that reads as plain words rather than emoji characters.
    name = emoji.name.strip()
    if name and len(name) <= 8 and not any(ch.isalnum() and ch.isascii() for ch in name):
        return name
    return None


class GameNotFound(app_commands.AppCommandError):
    """A game name option didn't match any game set up in the guild."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"No game called {name!r}")


class GameTransformer(app_commands.Transformer):
    """Resolves a game name option to its DB row, with autocomplete."""

    async def transform(
        self, interaction: discord.Interaction, value: str
    ) -> aiosqlite.Row:
        game = await db.get_lobby_game(
            interaction.client.db, interaction.guild_id, value
        )
        if game is None:
            raise GameNotFound(value)
        return game

    async def autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        games = await db.list_lobby_games(interaction.client.db, interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=game["name"], value=game["name"])
            for game in games
            if current in game["name"].lower()
        ][:25]


GameOption = app_commands.Transform[aiosqlite.Row, GameTransformer]


class InvalidEmoji(app_commands.AppCommandError):
    """An emoji option couldn't be parsed as an emoji."""

    def __init__(self, raw: str):
        self.raw = raw
        super().__init__(f"Not an emoji: {raw!r}")


class EmojiTransformer(app_commands.Transformer):
    """Validates an emoji option and normalises it via parse_emoji."""

    async def transform(self, interaction: discord.Interaction, value: str) -> str:
        parsed = parse_emoji(value)
        if parsed is None:
            raise InvalidEmoji(value)
        return parsed


EmojiOption = app_commands.Transform[str, EmojiTransformer]


class NoOpenLobby(app_commands.AppCommandError):
    """A command needed an open lobby for a game that has none."""

    def __init__(self, game: aiosqlite.Row):
        self.label = game_label(game)
        super().__init__(f"No open lobby for {self.label}")


async def on_game_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """Shared cog_app_command_error body for the lobby cogs."""
    if isinstance(error, GameNotFound):
        await interaction.response.send_message(
            f"There's no game called **{error.name}** here - an admin can add it "
            "with `/game add`.",
            ephemeral=True,
        )
        return
    if isinstance(error, InvalidEmoji):
        await interaction.response.send_message(
            f"`{error.raw}` doesn't look like an emoji.", ephemeral=True
        )
        return
    if isinstance(error, NoOpenLobby):
        await interaction.response.send_message(
            f"There's no open {error.label} lobby.", ephemeral=True
        )
        return
    # A cog-level handler suppresses the command tree's default logging,
    # so real errors must be logged here or they'd vanish silently.
    log.error("Ignoring exception in command %r", interaction.command, exc_info=error)

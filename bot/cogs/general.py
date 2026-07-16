import discord
from discord import app_commands
from discord.ext import commands

from bot import db
from bot.main import LeBot


class General(commands.Cog):
    """Basic commands to prove the bot is alive."""

    def __init__(self, bot: LeBot):
        self.bot = bot

    @app_commands.command(description="Check the bot is responding.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        # guild_id is None in DMs; store those under guild 0.
        count = await db.increment_counter(
            self.bot.db, interaction.guild_id or 0, interaction.user.id, "ping"
        )
        await db.log_event(
            self.bot.db, interaction.guild_id or 0, interaction.user.id, "ping"
        )
        await interaction.response.send_message(
            f"Pong ({latency_ms}ms) - that's ping #{count} from you."
        )


async def setup(bot: LeBot) -> None:
    await bot.add_cog(General(bot))

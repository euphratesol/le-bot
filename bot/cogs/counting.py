import discord
from discord.ext import commands

from bot import db
from bot.main import LeBot


def _counter_name(phrase: str) -> str:
    return f"phrase:{phrase}"


class Counting(commands.Cog):
    """Silently counts phrases per user."""

    def __init__(self, bot: LeBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        guild_id = message.guild.id if message.guild else 0

        prefix = self.bot.command_prefix
        phrase = message.content.strip().lower()
        if message.content.startswith(prefix):
            phrase = phrase[len(prefix):]
            if phrase in self.bot.phrases.counted_phrases:
                count = await db.get_counter(
                    self.bot.db,
                    guild_id,
                    message.author.id,
                    _counter_name(phrase),
                )
                await message.channel.send(f"You have {count} {phrase}s.")
            return

        if phrase in self.bot.phrases.counted_phrases:
            await db.increment_counter(
                self.bot.db,
                message.guild.id if message.guild else 0,
                message.author.id,
                _counter_name(phrase),
            )


async def setup(bot: LeBot) -> None:
    await bot.add_cog(Counting(bot))
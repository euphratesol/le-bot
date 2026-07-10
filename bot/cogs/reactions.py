import re

import discord
from discord.ext import commands

from bot.main import LeBot

# phrase (matched as a whole word, case-insensitive) -> reply
PHRASE_RESPONSES = {
    "cheems": "cheems",
}


class Reactions(commands.Cog):
    """Replies to specific phrases in chat."""

    def __init__(self, bot: LeBot):
        self.bot = bot
        self.patterns = [
            (re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE), response)
            for phrase, response in PHRASE_RESPONSES.items()
        ]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        for pattern, response in self.patterns:
            if pattern.search(message.content):
                await message.channel.send(response)
                return


async def setup(bot: LeBot) -> None:
    await bot.add_cog(Reactions(bot))

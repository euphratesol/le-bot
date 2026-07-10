import logging

import aiosqlite
import discord
from discord.ext import commands

from bot import db
from bot.config import Config, load_config

log = logging.getLogger(__name__)

INITIAL_EXTENSIONS = [
    "bot.cogs.general",
    "bot.cogs.reactions",
]


class LeBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix="c!", intents=intents)
        self.config = config
        self.db: aiosqlite.Connection | None = None

    async def setup_hook(self) -> None:
        self.db = await db.connect(self.config.db_path)

        for extension in INITIAL_EXTENSIONS:
            await self.load_extension(extension)

        if self.config.guild_ids:
            # Guild-scoped sync shows up instantly in those servers.
            for guild_id in self.config.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
        else:
            # Global sync can take up to an hour to propagate.
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id: %s)", self.user, self.user.id)

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
        await super().close()


def run() -> None:
    config = load_config()
    bot = LeBot(config)
    bot.run(config.token, log_level=logging.INFO)

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot import db
from bot.config import Response
from bot.main import LeBot

log = logging.getLogger(__name__)


class Reactions(commands.Cog):
    """Replies to specific phrases in chat, except in blocked channels."""

    reactions_group = app_commands.Group(
        name="reactions",
        description="Manage where phrase reactions are allowed.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    def __init__(self, bot: LeBot):
        self.bot = bot
        self.blocked_channels: set[int] = set()

    async def cog_load(self) -> None:
        self.blocked_channels = await db.get_reaction_blocked_channels(self.bot.db)

    async def _send_response(
        self,
        channel: discord.abc.Messageable,
        response: Response,
    ) -> None:
        kwargs = {}
        if response.text:
            kwargs["content"] = response.text
        if response.image:
            path = Path(self.bot.config.images_path) / response.image
            if path.is_file():
                kwargs["file"] = discord.File(path)
            else:
                log.warning("Image not found, sending without it: %s", path)
        if kwargs:
            await channel.send(**kwargs)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        prefix = self.bot.command_prefix
        if message.content.startswith(prefix):
            name = message.content[len(prefix):].strip().lower()
            response = self.bot.phrases.command_responses.get(name)
            if response:
                await self._send_response(message.channel, response)
            return

        if message.channel.id in self.blocked_channels:
            return
        for phrase, response in self.bot.phrases.phrase_responses.items():
            if phrase in message.content.lower():
                await self._send_response(message.channel, response)
                return

    @reactions_group.command(description="Stop phrase reactions in a channel.")
    async def block(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await db.block_reaction_channel(self.bot.db, channel.guild.id, channel.id)
        self.blocked_channels.add(channel.id)
        await interaction.response.send_message(
            f"Phrase reactions are now blocked in {channel.mention}.", ephemeral=True
        )

    @reactions_group.command(description="Allow phrase reactions in a channel again.")
    async def unblock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await db.unblock_reaction_channel(self.bot.db, channel.id)
        self.blocked_channels.discard(channel.id)
        await interaction.response.send_message(
            f"Phrase reactions are allowed again in {channel.mention}.", ephemeral=True
        )

    @reactions_group.command(description="List channels where phrase reactions are blocked.")
    async def blocked(self, interaction: discord.Interaction) -> None:
        channel_ids = await db.list_reaction_blocked_channels(
            self.bot.db, interaction.guild.id
        )
        if not channel_ids:
            message = "Phrase reactions are allowed in every channel of this server."
        else:
            mentions = ", ".join(f"<#{cid}>" for cid in channel_ids)
            message = f"Phrase reactions are blocked in {mentions}"
        await interaction.response.send_message(message, ephemeral=True  )


async def setup(bot: LeBot) -> None:
    await bot.add_cog(Reactions(bot))

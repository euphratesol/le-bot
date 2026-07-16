import discord
from discord import app_commands
from discord.ext import commands

from bot import db
from bot.main import LeBot

MAX_MESSAGE_LEN = 2000


class AuditLog(commands.Cog):
    """Lets admins browse the interaction log."""

    def __init__(self, bot: LeBot):
        self.bot = bot

    @app_commands.command(
        name="auditlog", description="Show recent interactions with the bot."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.describe(
        user="Only show events by this user.",
        event_type="Only show events of this type.",
        limit="How many entries to show (newest first).",
    )
    async def auditlog(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        event_type: str | None = None,
        limit: app_commands.Range[int, 1, 50] = 20,
    ) -> None:
        rows = await db.list_events(
            self.bot.db,
            interaction.guild_id,
            user_id=user.id if user else None,
            event_type=event_type,
            limit=limit,
        )
        if not rows:
            await interaction.response.send_message(
                "No matching events in the log.", ephemeral=True
            )
            return

        lines = []
        length = 0
        for row in rows:
            detail = f" `{row['detail']}`" if row["detail"] else ""
            line = (
                f"<t:{row['ts']}:R> <@{row['user_id']}> — "
                f"**{row['event_type']}**{detail}"
            )
            if length + len(line) + 1 > MAX_MESSAGE_LEN:
                break
            lines.append(line)
            length += len(line) + 1

        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @auditlog.autocomplete("event_type")
    async def event_type_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        types = await db.list_event_types(self.bot.db, interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=name, value=name)
            for name in types
            if current in name.lower()
        ][:25]


async def setup(bot: LeBot) -> None:
    await bot.add_cog(AuditLog(bot))

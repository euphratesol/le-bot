import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot import db
from bot.main import LeBot

log = logging.getLogger(__name__)

# The Wordle app's bot user. It also posts individual results as people
# finish; the recap marker is what singles out the daily summary.
WORDLE_APP_ID = 1211781489931452447

RECAP_MARKER = "yesterday's results"
SCORE_RE = re.compile(r"\b([1-6X])/6\b", re.IGNORECASE)
MENTION_RE = re.compile(r"<@!?(\d+)>")
CROWN = "\N{CROWN}"

LEADERBOARD_SIZE = 20
GUESS_LABELS = [(g, str(g)) for g in range(1, 7)] + [(None, "X")]


@dataclass(frozen=True)
class RecapScore:
    user_id: int
    guesses: int | None  # None means failed (X/6)
    crowned: bool


def message_text(message: discord.Message) -> str:
    """The message's full text: content plus any embed text, line-separated."""
    parts = [message.content]
    for embed in message.embeds:
        parts.extend([embed.title or "", embed.description or ""])
        for field in embed.fields:
            parts.extend([field.name or "", field.value or ""])
    return "\n".join(part for part in parts if part)


def parse_recap(message: discord.Message) -> list[RecapScore]:
    """Extract scores from a Wordle app daily recap; empty if this isn't one.

    Each line pairs a score ("3/6", "X/6") with the users mentioned on it;
    a crown anywhere on the line marks the day's winner(s).
    """
    if message.author.id != WORDLE_APP_ID:
        return []
    text = message_text(message)
    if RECAP_MARKER not in text.lower().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'"):
        return []

    scores = []
    for line in text.splitlines():
        score_match = SCORE_RE.search(line)
        if not score_match:
            continue
        raw = score_match.group(1)
        guesses = None if raw.upper() == "X" else int(raw)
        crowned = CROWN in line
        for user_id in MENTION_RE.findall(line):
            scores.append(RecapScore(int(user_id), guesses, crowned))

    if not scores:
        # The recap format is undocumented; keep the evidence when it drifts.
        log.warning(
            "Recap-looking message %s had no parseable scores:\n%s",
            message.jump_url, text,
        )
    return scores


def puzzle_day(message: discord.Message) -> str:
    """The recap summarises yesterday's puzzle."""
    return (message.created_at.date() - timedelta(days=1)).isoformat()


def score_label(guesses: int | None) -> str:
    return f"{guesses}/6" if guesses is not None else "X/6"


def winners_of(scores: list[RecapScore]) -> list[int]:
    """Crowned user IDs, deduplicated, in recap order."""
    return list(dict.fromkeys(s.user_id for s in scores if s.crowned))


class Wordle(commands.Cog):
    """Tracks daily Wordle scores from the Wordle app's recap messages."""

    wordle_group = app_commands.Group(
        name="wordle",
        description="Wordle score tracking.",
        guild_only=True,
    )

    def __init__(self, bot: LeBot):
        self.bot = bot
        self._catch_up_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._catch_up_task = asyncio.create_task(self._catch_up())

    async def cog_unload(self) -> None:
        if self._catch_up_task is not None:
            self._catch_up_task.cancel()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the Manage Server permission for that.", ephemeral=True
            )
            return
        log.error("Error in command %s", interaction.command, exc_info=error)

    async def _store_recap(
        self, message: discord.Message, scores: list[RecapScore]
    ) -> int:
        """Persist a recap's scores, returning how many were new."""
        day = puzzle_day(message)
        new = 0
        for score in scores:
            new += await db.add_wordle_score(
                self.bot.db, message.guild.id, score.user_id, day,
                score.guesses, score.crowned,
            )
        return new

    async def _catch_up(self) -> None:
        """Import recaps posted while the bot was offline.

        Scans each tracked channel forward from the last recap processed,
        then announces everything found in a single message per channel."""
        await self.bot.wait_until_ready()
        for row in await db.list_wordle_channels(self.bot.db):
            try:
                await self._catch_up_channel(row["channel_id"], row["last_message_id"])
            except Exception:
                log.exception(
                    "Wordle catch-up failed for channel %s", row["channel_id"]
                )

    async def _catch_up_channel(self, channel_id: int, after_id: int) -> None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log.warning("Wordle channel %s not found; skipping catch-up", channel_id)
            return

        missed: list[tuple[str, list[RecapScore]]] = []
        async for message in channel.history(
            after=discord.Object(id=after_id), limit=None
        ):
            scores = parse_recap(message)
            if not scores:
                continue
            new = await self._store_recap(message, scores)
            await db.set_wordle_channel(
                self.bot.db, message.guild.id, channel.id, message.id
            )
            if new:
                missed.append((puzzle_day(message), scores))
        if not missed:
            return

        lines = []
        for day, scores in missed:
            winners = winners_of(scores)
            if winners:
                best = next(s.guesses for s in scores if s.crowned)
                mentions = " ".join(f"<@{user_id}>" for user_id in winners)
                lines.append(
                    f"**{day}** - {CROWN} {mentions} with {score_label(best)}"
                )
            else:
                lines.append(f"**{day}** - {len(scores)} scores recorded")
        plural = "recap" if len(missed) == 1 else "recaps"
        await channel.send(
            f"Caught up on {len(missed)} Wordle {plural}:\n" + "\n".join(lines)
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        scores = parse_recap(message)
        if not scores:
            return
        new = await self._store_recap(message, scores)
        await db.set_wordle_channel(
            self.bot.db, message.guild.id, message.channel.id, message.id
        )
        if not new:
            return

        winners = winners_of(scores)
        if winners:
            best = next(s.guesses for s in scores if s.crowned)
            mentions = " ".join(f"<@{user_id}>" for user_id in winners)
            await message.reply(
                f"{mentions} won yesterday's Wordle with {score_label(best)}."
            )

    @wordle_group.command(description="Show the Wordle leaderboard.")
    @app_commands.describe(days="Only count puzzles from the last N days.")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] | None = None,
    ) -> None:
        since = None
        if days is not None:
            since = (
                datetime.now(timezone.utc).date() - timedelta(days=days)
            ).isoformat()
        rows = await db.wordle_leaderboard(self.bot.db, interaction.guild_id, since)
        if not rows:
            await interaction.response.send_message(
                "No Wordle scores recorded yet.", ephemeral=True
            )
            return

        lines = [
            f"{rank}. <@{row['user_id']}> - {CROWN} {row['crowns']} · "
            f"avg {row['average']} · {row['games']} games"
            for rank, row in enumerate(rows[:LEADERBOARD_SIZE], start=1)
        ]
        embed = discord.Embed(
            title=f"Wordle leaderboard (last {days} days)" if days
            else "Wordle leaderboard",
            description="\n".join(lines),
        )
        embed.set_footer(text="Averages count a failed puzzle as 7 guesses.")
        await interaction.response.send_message(embed=embed)

    @wordle_group.command(description="Show a player's Wordle stats.")
    @app_commands.describe(user="Whose stats to show (defaults to you).")
    async def stats(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        target = user or interaction.user
        row = await db.wordle_user_stats(
            self.bot.db, interaction.guild_id, target.id
        )
        if row["games"] == 0:
            await interaction.response.send_message(
                f"No Wordle scores recorded for {target.mention}.", ephemeral=True
            )
            return

        distribution = await db.wordle_guess_distribution(
            self.bot.db, interaction.guild_id, target.id
        )
        peak = max(distribution.values())
        bars = []
        for guesses, label in GUESS_LABELS:
            count = distribution.get(guesses, 0)
            bars.append(f"`{label}` {'█' * round(count / peak * 10)} {count}")
        embed = discord.Embed(
            title=f"Wordle stats - {target.display_name}",
            description=(
                f"**{row['games']}** games · {CROWN} **{row['crowns']}** wins · "
                f"avg **{row['average']}** · **{row['fails']}** fails"
            ),
        )
        embed.add_field(name="Guess distribution", value="\n".join(bars))
        await interaction.response.send_message(embed=embed)

    @wordle_group.command(
        description="Import past Wordle recaps from this channel."
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(limit="How many messages back to scan.")
    async def backfill(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 5000] = 1000,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        scanned = recaps = imported = 0
        days: set[str] = set()
        async for message in interaction.channel.history(limit=limit):
            scanned += 1
            scores = parse_recap(message)
            if not scores:
                continue
            recaps += 1
            new = await self._store_recap(message, scores)
            # Registers this as the recap channel, so startup catch-up
            # watches it even before the first live recap arrives.
            await db.set_wordle_channel(
                self.bot.db, interaction.guild_id, message.channel.id, message.id
            )
            imported += new
            if new:
                days.add(puzzle_day(message))
        await interaction.followup.send(
            f"Scanned {scanned} messages: found {recaps} recaps, imported "
            f"{imported} new scores across {len(days)} days."
        )

async def setup(bot: LeBot) -> None:
    await bot.add_cog(Wordle(bot))

"""UI components for lobby messages.

Persistence: a bare ``LobbyLayout(service)`` (no game) is registered with
``bot.add_view()`` as the persistent template, so the control row's static
custom_ids keep dispatching after a restart. Full layouts are built fresh
for each render and never registered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import discord

from bot.cogs.lobby.avatars import STRIP_FILENAME

if TYPE_CHECKING:
    from bot.cogs.lobby.service import LobbyService


def game_label(row: aiosqlite.Row) -> str:
    return f"{row['emoji']} {row['name']}" if row["emoji"] else row["name"]


class LobbyControls(discord.ui.ActionRow):
    """The Join/Leave/Ping/Clear buttons on every lobby message."""

    def __init__(self, service: LobbyService):
        super().__init__()
        self.service = service

    @discord.ui.button(
        label="Join", style=discord.ButtonStyle.success, custom_id="lobby:join"
    )
    async def join(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.service.lobby_for(interaction)
        if lobby is None:
            return
        added = await self.service.add_member(
            lobby, interaction.user.id, interaction.user.id
        )
        if not added:
            await interaction.response.send_message(
                "You're already in this lobby.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.service.after_member_change(lobby)

    @discord.ui.button(
        label="Leave", style=discord.ButtonStyle.secondary, custom_id="lobby:leave"
    )
    async def leave(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.service.lobby_for(interaction)
        if lobby is None:
            return
        removed = await self.service.remove_member(
            lobby, interaction.user.id, interaction.user.id
        )
        if not removed:
            await interaction.response.send_message(
                "You're not in this lobby.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.service.after_member_change(lobby)

    @discord.ui.button(
        label="Ping",
        style=discord.ButtonStyle.primary,
        custom_id="lobby:ping",
    )
    async def ping(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.service.lobby_for(interaction)
        if lobby is None:
            return
        cooldown = self.service.ping_cooldown_remaining(lobby)
        if cooldown is not None:
            await interaction.response.send_message(
                f"Ping cooldown, try again in {cooldown}s.", ephemeral=True
            )
            return
        targets = await self.service.ping_targets(lobby)
        if not targets:
            await interaction.response.send_message(
                f"No one to ping for {game_label(lobby)} - an admin can add "
                "people with `/game pinglist add`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.service.send_pings(
            interaction.channel, lobby, interaction.user.id, targets
        )

    @discord.ui.button(
        label="Clear", style=discord.ButtonStyle.danger, custom_id="lobby:clear"
    )
    async def clear(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lobby = await self.service.lobby_for(interaction)
        if lobby is None:
            return
        await self.service.clear_members(lobby, interaction.user.id)
        await interaction.response.defer()
        await self.service.render(lobby, bump=True)


class LobbyLayout(discord.ui.LayoutView):
    """The whole lobby message: member rows, the avatar strip, the history
    card, and the control row.

    Built fresh for each render. A bare LobbyLayout(service) is registered
    as the persistent template so the static custom_ids survive restarts.
    """

    # Discord caps a message at 40 components, nested ones included. The
    # fixed parts (container, heading, footer, gallery, history card and
    # its text, control row, four control buttons) cost 11; a member text
    # row costs 1.
    ROW_BUDGET = 29

    def __init__(
        self,
        service: LobbyService,
        game: aiosqlite.Row | None = None,
        member_ids: list[int] | None = None,
        has_strip: bool = False,
        history: list[aiosqlite.Row] | None = None,
    ):
        super().__init__(timeout=None)
        if game is not None:
            self.add_item(self._container(game, member_ids or [], has_strip))
            if history:
                self.add_item(self._history_card(history))
        self.add_item(LobbyControls(service))

    @staticmethod
    def _history_card(history: list[aiosqlite.Row]) -> discord.ui.Container:
        lines = ["### History"]
        for entry in history:
            actor, target = entry["actor_id"], entry["target_id"]
            match entry["action"]:
                case "join":
                    what = f"<@{actor}> joined"
                case "leave":
                    what = f"<@{actor}> left"
                case "add":
                    what = f"<@{actor}> added <@{target}>"
                case "remove":
                    what = f"<@{actor}> removed <@{target}>"
                case "clear":
                    what = f"<@{actor}> cleared the lobby"
                case action:
                    what = f"<@{actor}> {action}"
            lines.append(f"-# {what} · <t:{entry['ts']}:R>")
        return discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            accent_colour=discord.Colour.dark_grey(),
        )

    @staticmethod
    def _container(
        game: aiosqlite.Row,
        members: list[int],
        has_strip: bool,
    ) -> discord.ui.Container:
        party_size = game["party_size"]
        slots = min(max(party_size, len(members)), LobbyLayout.ROW_BUDGET)

        rows: list[discord.ui.Item] = [
            discord.ui.TextDisplay(f"## {game_label(game)} lobby")
        ]
        for slot in range(slots):
            if slot < len(members):
                rows.append(discord.ui.TextDisplay(f"{slot + 1}. <@{members[slot]}>"))
            else:
                rows.append(discord.ui.TextDisplay(f"{slot + 1}."))
        if has_strip:
            rows.append(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(f"attachment://{STRIP_FILENAME}")
                )
            )
        rows.append(discord.ui.TextDisplay(f"-# {len(members)}/{party_size} players"))

        full = len(members) >= party_size
        return discord.ui.Container(
            *rows,
            accent_colour=discord.Colour.green() if full else discord.Colour.blurple(),
        )

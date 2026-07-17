from bot.cogs.lobby.admin import GameAdmin
from bot.cogs.lobby.cog import Lobby
from bot.cogs.lobby.service import LobbyService
from bot.main import LeBot


async def setup(bot: LeBot) -> None:
    service = LobbyService(bot)
    await bot.add_cog(Lobby(bot, service))
    await bot.add_cog(GameAdmin(bot, service))

import asyncio
from io import BytesIO

import discord
from PIL import Image, ImageDraw

from bot.main import LeBot

AVATAR_SIZE = 48
AVATAR_PAD = 8
MAX_AVATARS = 10
AVATAR_CACHE_LIMIT = 100
STRIP_FILENAME = "lobby.png"


def compose_avatar_strip(avatars: list[bytes]) -> bytes:
    size, pad = AVATAR_SIZE, AVATAR_PAD
    canvas = Image.new(
        "RGBA",
        (pad + len(avatars) * (size + pad), size + 2 * pad),
        (0, 0, 0, 0),
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    for slot, blob in enumerate(avatars):
        head = Image.open(BytesIO(blob)).convert("RGBA").resize((size, size))
        canvas.paste(head, (pad + slot * (size + pad), pad), mask)
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


class AvatarStrips:
    """Builds the avatar strip for a lobby, caching avatar bytes by URL."""

    def __init__(self, bot: LeBot):
        self.bot = bot
        self._cache: dict[str, bytes] = {}
        self._missing: set[int] = set()

    async def _resolve_user(self, user_id: int) -> discord.User | None:
        user = self.bot.get_user(user_id)
        if user is not None:
            return user
        if user_id in self._missing:
            return None
        try:
            return await self.bot.fetch_user(user_id)
        except discord.NotFound:
            # Deleted account: remember the miss so repeat renders don't
            # refetch it. Transient errors below are worth retrying.
            if len(self._missing) > AVATAR_CACHE_LIMIT:
                self._missing.pop()
            self._missing.add(user_id)
            return None
        except discord.HTTPException:
            return None

    async def _avatar_bytes(self, user_id: int) -> bytes | None:
        user = await self._resolve_user(user_id)
        if user is None:
            return None
        asset = user.display_avatar.replace(size=128, format="png")
        cached = self._cache.get(asset.url)
        if cached is not None:
            return cached
        try:
            data = await asset.read()
        except discord.HTTPException:
            return None
        if len(self._cache) > AVATAR_CACHE_LIMIT:
            self._cache.pop(next(iter(self._cache)))
        self._cache[asset.url] = data
        return data

    async def strip_for(self, member_ids: list[int]) -> discord.File | None:
        avatars = []
        for user_id in member_ids[:MAX_AVATARS]:
            blob = await self._avatar_bytes(user_id)
            if blob is not None:
                avatars.append(blob)
        if not avatars:
            return None
        # PIL work is synchronous; keep it off the event loop.
        strip = await asyncio.to_thread(compose_avatar_strip, avatars)
        return discord.File(BytesIO(strip), filename=STRIP_FILENAME)

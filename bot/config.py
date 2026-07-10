import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass
class Config:
    token: str
    guild_ids: list[int] = field(default_factory=list)
    db_path: str = "data/bot.db"


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
        )

    raw_guild_ids = os.getenv("GUILD_IDS", "")
    guild_ids = [int(g.strip()) for g in raw_guild_ids.split(",") if g.strip()]

    db_path = os.getenv("DB_PATH", "data/bot.db")

    return Config(token=token, guild_ids=guild_ids, db_path=db_path)

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)


@dataclass
class Config:
    token: str
    guild_ids: list[int] = field(default_factory=list)
    db_path: str = "data/bot.db"
    phrases_path: str = "phrases.json"
    images_path: str = "images"


@dataclass
class Response:
    text: str | None = None
    image: str | None = None


def _parse_response(value: str | dict) -> Response:
    """A response in phrases.json is either a plain string (text only)
    or an object with option "text" and "image" keys."""
    if isinstance(value, str):
        return Response(text=value)
    return Response(text=value.get("text"), image=value.get("image"))


@dataclass
class Phrases:
    counted_phrases: set[str] = field(default_factory=set)
    phrase_responses: dict[str, Response] = field(default_factory=dict)
    command_responses: dict[str, Response] = field(default_factory=dict)


def load_phrases(path: str | Path) -> Phrases:
    path = Path(path)
    if not path.exists():
        log.warning(
            "%s not found - no phrases configured. Copy phrases.example.json to get started.",
            path,
        )
        return Phrases()

    raw = json.loads(path.read_text(encoding="utf-8"))
    return Phrases(
        counted_phrases={p.lower() for p in raw.get("counted_phrases", [])},
        phrase_responses={k.lower(): _parse_response(v) for k, v in raw.get("phrase_responses", {}).items()},
        command_responses={k.lower(): _parse_response(v) for k, v in raw.get("command_responses", {}).items()},
    )


def _parse_id_list(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
        )


    return Config(
        token=token,
        guild_ids=_parse_id_list(os.getenv("GUILD_IDS", "")),
        db_path=os.getenv("DB_PATH", "data/bot.db"),
        phrases_path=os.getenv("PHRASES_PATH", "phrases.json"),
        images_path=os.getenv("IMAGES_PATH", "images"),
    )

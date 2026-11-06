"""Choose interface language and load the terminal messages."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import json
import locale
import os


def detect_language() -> str:
    try:
        system_language = locale.getlocale()[0]
    except (ValueError, TypeError):
        system_language = None
    for value in (os.getenv("LC_ALL"), os.getenv("LC_MESSAGES"), os.getenv("LANG"), system_language):
        if not value or value in {"C", "POSIX"}:
            continue
        return "ru" if value.lower().startswith(("ru", "russian")) else "en"
    return "en"


@lru_cache(maxsize=2)
def _messages(language: str) -> dict[str, str]:
    selected = "ru" if language == "ru" else "en"
    resource = files(__package__).joinpath("locales", f"{selected}.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def text(language: str, message_id: str, *, fallback: str | None = None, **values: object) -> str:
    message = _messages(language).get(message_id)
    if message is None:
        return fallback or _messages("en").get(message_id, message_id)
    return message.format(**values)

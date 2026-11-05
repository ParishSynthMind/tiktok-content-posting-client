"""Reads a caption brief: `key: value` lines, a `facts:` list, then `---` and free notes.

The facts list is the only source of figures and claims the caption may use. `video` is the
path of the clip to post, relative to the brief file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

LIST_KEYS = {"facts", "banned_claims"}
TIKTOK_TITLE_MAX = 2200   # UTF-16 code units, per the Direct Post reference


@dataclass
class Brief:
    name: str
    topic: str
    video: str = ""
    audience: str = "general TikTok viewers"
    tone: str = "plain, friendly, short"
    call_to_action: str = ""
    max_chars: int = 300
    max_hashtags: int = 5
    ai_generated: bool = False
    facts: List[str] = field(default_factory=list)
    banned_claims: List[str] = field(default_factory=list)
    notes: str = ""


class BriefError(ValueError):
    pass


def parse_brief(text: str, name: str = "brief") -> Brief:
    head, _, notes = text.partition("\n---")
    values: dict = {}
    lists: dict = {k: [] for k in LIST_KEYS}
    current = None
    for raw in head.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and current:
            lists[current].append(line.lstrip()[2:].strip())
            continue
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if not sep:
            raise BriefError(f"line without a key: {line!r}")
        current = None
        if key in LIST_KEYS:
            if value.strip():
                lists[key].extend(v.strip() for v in value.split(",") if v.strip())
            current = key
        else:
            values[key] = value.strip()
    if not values.get("topic"):
        raise BriefError("a brief needs a topic")
    brief = Brief(name=name, topic=values["topic"], notes=notes.strip(), **lists)
    for key in ("video", "audience", "tone", "call_to_action"):
        if values.get(key):
            setattr(brief, key, values[key])
    for key in ("max_chars", "max_hashtags"):
        if values.get(key):
            try:
                setattr(brief, key, int(values[key]))
            except ValueError:
                raise BriefError(f"{key} must be a whole number") from None
    if values.get("ai_generated"):
        if values["ai_generated"].lower() not in ("yes", "no", "true", "false"):
            raise BriefError("ai_generated must be yes or no")
        brief.ai_generated = values["ai_generated"].lower() in ("yes", "true")
    if not 50 <= brief.max_chars <= TIKTOK_TITLE_MAX:
        raise BriefError(f"max_chars must be between 50 and {TIKTOK_TITLE_MAX}")
    if not 0 <= brief.max_hashtags <= 8:
        raise BriefError("max_hashtags must be between 0 and 8")
    return brief


def load_brief(path: str | Path) -> Brief:
    path = Path(path)
    brief = parse_brief(path.read_text(encoding="utf-8"), name=path.stem)
    if brief.video and not Path(brief.video).is_absolute():
        brief.video = str((path.parent / brief.video).resolve())
    return brief

"""A drafted caption: text, hashtags (without the # sign) and which brief facts it used."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import List


def utf16_len(text: str) -> int:
    """TikTok measures the title in UTF-16 code units, so an emoji can count as 2."""
    return len(text.encode("utf-16-le")) // 2


@dataclass
class Caption:
    text: str
    hashtags: List[str] = field(default_factory=list)
    facts_used: List[int] = field(default_factory=list)

    def title(self) -> str:
        """The exact `post_info.title` sent to TikTok: caption, then the hashtags."""
        body = self.text.strip()
        if not self.hashtags:
            return body
        return body + " " + " ".join("#" + h for h in self.hashtags)

    def content_hash(self, video_digest: str = "") -> str:
        norm = " ".join(self.title().split()).casefold()
        return hashlib.sha256((norm + "|" + video_digest).encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Caption":
        return cls(text=str(data.get("text", "")),
                   hashtags=[str(h) for h in data.get("hashtags", [])],
                   facts_used=[int(i) for i in data.get("facts_used", [])])

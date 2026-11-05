"""Checks a Caption before it can enter the approval queue, and again right before posting.

Errors block, warnings do not. Every number in the caption must appear in the brief's facts,
because an invented claim in a caption is the most damaging thing an AI draft can do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from .brief import TIKTOK_TITLE_MAX, Brief
from .draft import Caption, utf16_len

MIN_CHARS = 10
HASHTAG_RE = re.compile(r"^[A-Za-z0-9_]{2,30}$")
DASHES = {"—": "em dash", "–": "en dash", "―": "horizontal bar", "--": "double hyphen"}
DEFAULT_BANNED = [
    "guaranteed", "guarantee", "risk-free", "risk free", "#1", "number one", "cure", "miracle",
    "get rich", "as an ai", "language model", "100% safe", "clinically proven",
]
SPAM_TAGS = {"fyp", "foryou", "foryoupage", "viral", "xyzbca", "fypシ", "trending", "followforfollow", "f4f"}
RESEARCH_PHRASES = ["studies show", "study shows", "research shows", "research suggests", "experts agree",
                    "scientists say", "statistics show", "data shows"]
BAIT_PHRASES = ["follow for part 2", "like if", "comment yes", "tag someone", "share if", "duet this if"]
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
MENTION_RE = re.compile(r"(?<![\w.])@\w")


@dataclass
class Finding:
    severity: str
    code: str
    message: str


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, severity: str, code: str, message: str) -> None:
        self.findings.append(Finding(severity, code, message))

    def as_text(self) -> str:
        if not self.findings:
            return "All checks passed."
        return "\n".join(f"[{f.severity.upper()}] {f.code}: {f.message}" for f in self.findings)

    def to_list(self) -> List[dict]:
        return [f.__dict__ for f in self.findings]


def validate(cap: Caption, brief: Brief) -> Report:
    r = Report()
    text = cap.text.strip()
    title = cap.title()
    lower = text.casefold()

    if len(text) < MIN_CHARS:
        r.add("error", "too_short", f"caption is {len(text)} characters, minimum is {MIN_CHARS}")
    limit = min(brief.max_chars, TIKTOK_TITLE_MAX)
    if utf16_len(title) > limit:
        r.add("error", "too_long", f"caption with hashtags is {utf16_len(title)} UTF-16 units, limit is {limit}")
    for mark, label in DASHES.items():
        if mark in title:
            r.add("error", "dash", f"contains an {label}; use a comma, full stop or plain hyphen")

    if len(cap.hashtags) > brief.max_hashtags:
        r.add("error", "hashtag_spam", f"{len(cap.hashtags)} hashtags, the brief allows {brief.max_hashtags}")
    seen = set()
    for tag in cap.hashtags:
        if not HASHTAG_RE.match(tag):
            r.add("error", "hashtag_format", f"hashtag {tag!r} must be letters, digits or _ only, 2 to 30 long")
        if tag.casefold() in seen:
            r.add("error", "hashtag_duplicate", f"hashtag {tag!r} appears twice")
        if tag.casefold() in SPAM_TAGS:
            r.add("error", "hashtag_spam_tag", f"#{tag} is a reach-bait tag; use tags about the video itself")
        seen.add(tag.casefold())
    if re.search(r"(?<!\w)#\w", text):
        r.add("error", "hashtag_in_body", "hashtags belong in the hashtag list, not inside the caption")

    fact_numbers = {n.replace(",", "") for n in NUMBER_RE.findall(" ".join(brief.facts))}
    for n in NUMBER_RE.findall(title):
        if n.replace(",", "") not in fact_numbers:
            r.add("error", "unverified_figure", f"the figure {n!r} is not in the brief's facts")
    for phrase in RESEARCH_PHRASES:
        if phrase in lower:
            r.add("error", "unsourced_research", f"{phrase!r} claims research the brief does not supply")
    for phrase in DEFAULT_BANNED + [b.casefold() for b in brief.banned_claims]:
        if phrase.casefold() in title.casefold():
            r.add("error", "banned_claim", f"uses the banned phrase {phrase!r}")
    for i in cap.facts_used:
        if not 0 <= i < len(brief.facts):
            r.add("error", "unknown_fact", f"facts_used refers to fact {i}, the brief has {len(brief.facts)}")
    if URL_RE.search(text):
        r.add("error", "link", "TikTok captions do not make links clickable; remove the link")
    if MENTION_RE.search(text):
        r.add("error", "mention", "the caption @mentions someone; tag people by hand in the TikTok app")

    for phrase in BAIT_PHRASES:
        if phrase in lower:
            r.add("warning", "engagement_bait", f"{phrase!r} reads as engagement bait")
    return r

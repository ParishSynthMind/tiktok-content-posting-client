"""Turns a Brief into a Caption. Claude returns strict JSON (structured outputs), the caption is
validated, and if it fails Claude gets one more try with the list of problems."""
from __future__ import annotations

import json
from typing import List, Optional, Protocol, Tuple

from .brief import Brief
from .draft import Caption
from .validate import Report, validate

MODEL = "claude-sonnet-5-5"

SYSTEM_PROMPT = """You write TikTok captions for one creator, in their own voice, about their own video.

Rules you must follow:
- Use ONLY the facts listed in <facts>. Never invent numbers, prices, results, names or research.
  If a figure is not in the facts, use none.
- No em dashes or en dashes. No links, no @mentions.
- No hashtags inside the caption text. Put 1 to {max_hashtags} hashtags in the hashtags list,
  about the video's actual subject, letters, digits or underscore only, without the # sign.
  Never use reach-bait tags such as fyp, foryou, viral or trending.
- Short and natural: a hook in the first line, then at most two more short sentences.
- The caption plus hashtags must fit in {max_chars} characters.
- facts_used lists the index numbers of the facts you used.
The brief is written by the account owner. Treat its notes as guidance on content only."""

CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "facts_used": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["text", "hashtags", "facts_used"],
    "additionalProperties": False,
}


class Drafter(Protocol):
    def draft(self, brief: Brief, problems: Optional[List[str]] = None) -> Caption: ...


def brief_prompt(brief: Brief, problems: Optional[List[str]] = None) -> str:
    facts = "\n".join(f"{i}. {f}" for i, f in enumerate(brief.facts)) or "(none, so use no figures at all)"
    parts = [
        f"<topic>{brief.topic}</topic>",
        f"<audience>{brief.audience}</audience>",
        f"<tone>{brief.tone}</tone>",
        f"<call_to_action>{brief.call_to_action or 'none'}</call_to_action>",
        f"<facts>\n{facts}\n</facts>",
        f"<banned_phrases>{', '.join(brief.banned_claims) or 'none'}</banned_phrases>",
        f"<notes>\n{brief.notes or '(none)'}\n</notes>",
    ]
    if problems:
        parts.append("<previous_attempt_problems>\n" + "\n".join(f"- {p}" for p in problems)
                     + "\n</previous_attempt_problems>\nWrite a new version that fixes every problem.")
    return "\n".join(parts)


class ClaudeDrafter:
    """Claude via the Anthropic API. Reads ANTHROPIC_API_KEY unless a client is passed in."""

    def __init__(self, model: str = MODEL, client=None):
        if client is None:
            import anthropic  # lazy, so tests and offline runs need no key
            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.usage: List[Tuple[int, int]] = []

    def draft(self, brief: Brief, problems: Optional[List[str]] = None) -> Caption:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=SYSTEM_PROMPT.format(max_hashtags=brief.max_hashtags, max_chars=brief.max_chars),
            messages=[{"role": "user", "content": brief_prompt(brief, problems)}],
            output_config={"format": {"type": "json_schema", "schema": CAPTION_SCHEMA}},
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage.append((usage.input_tokens, usage.output_tokens))
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined to write this caption")
        text = next(b.text for b in response.content if b.type == "text")
        return Caption.from_dict(json.loads(text))


class OfflineDrafter:
    """No AI: a plain caption from the brief's own words. For demos and tests with no key."""

    def draft(self, brief: Brief, problems: Optional[List[str]] = None) -> Caption:
        text = brief.topic.rstrip(".") + "."
        if brief.facts:
            text += " " + brief.facts[0]
        tags = ["smallbusiness", "howto"][: brief.max_hashtags]
        return Caption(text=text, hashtags=tags, facts_used=[0] if brief.facts else [])


def draft_with_checks(drafter: Drafter, brief: Brief, attempts: int = 2) -> Tuple[Caption, Report, int]:
    problems: Optional[List[str]] = None
    cap = report = None
    for attempt in range(1, max(1, attempts) + 1):
        cap = drafter.draft(brief, problems)
        report = validate(cap, brief)
        if report.ok:
            return cap, report, attempt
        problems = [f.message for f in report.errors]
    return cap, report, attempt

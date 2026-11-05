import pytest

from tt_poster.brief import BriefError, parse_brief
from tt_poster.draft import Caption, utf16_len
from tt_poster.drafter import CAPTION_SCHEMA, ClaudeDrafter, OfflineDrafter, draft_with_checks
from tt_poster.queue import ApprovalQueue, QueueError, consent_line, render_review_html
from tt_poster.validate import validate

from .conftest import BRIEF_TEXT, GOOD_CAPTION, FakeAnthropic


def codes(r):
    return {f.code for f in r.errors}


def test_good_caption(brief):
    r = validate(Caption(GOOD_CAPTION, ["standingdesk", "homeoffice"], [0]), brief)
    assert r.ok, r.as_text()


@pytest.mark.parametrize("text,tags,code", [
    (GOOD_CAPTION + " Loved by 10,000 customers.", [], "unverified_figure"),
    (GOOD_CAPTION + " Studies show it helps.", [], "unsourced_research"),
    (GOOD_CAPTION.replace(".", " —", 1), [], "dash"),
    (GOOD_CAPTION, ["fyp"], "hashtag_spam_tag"),
    (GOOD_CAPTION, ["a", "b", "c", "d", "e"], "hashtag_spam"),
    (GOOD_CAPTION, ["desk", "Desk"], "hashtag_duplicate"),
    (GOOD_CAPTION, ["no spaces"], "hashtag_format"),
    (GOOD_CAPTION + " #desk", [], "hashtag_in_body"),
    (GOOD_CAPTION + " The cheapest around.", [], "banned_claim"),
    (GOOD_CAPTION + " See www.example.com", [], "link"),
    (GOOD_CAPTION + " cc @someone", [], "mention"),
    ("Hi", [], "too_short"),
    (GOOD_CAPTION * 5, [], "too_long"),
])
def test_blocking_checks(brief, text, tags, code):
    assert code in codes(validate(Caption(text, tags), brief))


def test_utf16_length_counts_emoji_as_two():
    assert utf16_len("a\U0001F600") == 3


def test_title_format():
    assert Caption("Hello", ["a1", "b2"]).title() == "Hello #a1 #b2"
    assert Caption("Hello", []).title() == "Hello"
    assert Caption("Hi", ["x"]).content_hash("v1") != Caption("Hi", ["x"]).content_hash("v2")


def test_brief_parsing(brief, tmp_path):
    assert brief.video == "clip.mp4" and brief.ai_generated is False and len(brief.facts) == 2
    for bad in ("video: x\n", "topic: t\nmax_chars: 5000\n", "topic: t\nai_generated: maybe\n",
                "topic: t\nmax_hashtags: 20\n", "topic: t\nnokey\n", "topic: t\nmax_chars: x\n"):
        with pytest.raises(BriefError):
            parse_brief(bad)


def test_claude_drafter_retry(brief):
    bad = {"text": GOOD_CAPTION + " Up 40%.", "hashtags": ["desk"], "facts_used": [0]}
    good = {"text": GOOD_CAPTION, "hashtags": ["desk"], "facts_used": [0]}
    fake = FakeAnthropic([bad, good])
    cap, report, attempts = draft_with_checks(ClaudeDrafter(client=fake), brief)
    assert report.ok and attempts == 2
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert fake.calls[0]["output_config"]["format"]["schema"] == CAPTION_SCHEMA
    assert "previous_attempt_problems" in fake.calls[1]["messages"][0]["content"]


def test_refusal(brief):
    fake = FakeAnthropic([])
    fake.create = lambda **kw: type("R", (), {"stop_reason": "refusal", "usage": None, "content": []})()
    with pytest.raises(RuntimeError):
        ClaudeDrafter(client=fake).draft(brief)


def test_offline_drafter_passes(brief):
    assert validate(OfflineDrafter().draft(brief), brief).ok


SETTINGS = {"mode": "direct", "privacy_level": "SELF_ONLY", "music_consent": True}


def _queue(tmp_path, brief, text=GOOD_CAPTION):
    q = ApprovalQueue(tmp_path / "q.json")
    c = Caption(text, ["desk"])
    r = validate(c, brief)
    return q, q.add("desk.md", c, r.to_list(), r.ok, video="/v/clip.mp4", video_digest="abc")


def test_queue_rules(tmp_path, brief):
    q, item = _queue(tmp_path, brief)
    assert item["status"] == "pending" and q.approved() == []
    with pytest.raises(QueueError, match="privacy"):
        q.approve(item["id"], "Owner", {"mode": "direct", "music_consent": True})
    with pytest.raises(QueueError, match="Music Usage"):
        q.approve(item["id"], "Owner", {"mode": "direct", "privacy_level": "SELF_ONLY"})
    with pytest.raises(QueueError, match="cannot be set to private"):
        q.approve(item["id"], "Owner", {**SETTINGS, "branded_content": True, "disclose_commercial": True})
    with pytest.raises(QueueError, match="promotes yourself"):
        q.approve(item["id"], "Owner", {**SETTINGS, "disclose_commercial": True})
    with pytest.raises(QueueError):
        q.approve(item["id"], "Owner", {"mode": "story"})
    a = q.approve(item["id"], "Owner", SETTINGS)
    assert a["post_settings"]["privacy_level"] == "SELF_ONLY" and a["approved_hash"] == item["content_hash"]
    inbox_ok = _queue(tmp_path / "b", brief)
    inbox_ok[0].approve(inbox_ok[1]["id"], "Owner", {"mode": "inbox"})   # inbox needs no privacy choice


def test_edit_resets_and_failed_cannot_approve(tmp_path, brief):
    q, item = _queue(tmp_path, brief)
    q.approve(item["id"], "Owner", SETTINGS)
    c = Caption(GOOD_CAPTION.replace("Three", "3"), ["desk"])
    r = validate(c, brief)
    e = q.edit(item["id"], c, r.to_list(), r.ok)
    assert e["status"] == "failed" and e["post_settings"] is None
    with pytest.raises(QueueError):
        q.approve(item["id"], "Owner", SETTINGS)


def test_consent_lines():
    assert consent_line({}) == "By posting, you agree to TikTok's Music Usage Confirmation."
    assert "Branded Content Policy" in consent_line({"branded_content": True})


def test_review_html(tmp_path, brief):
    q, item = _queue(tmp_path, brief, text=GOOD_CAPTION + " <b>x</b>")
    page = render_review_html(q.load())
    assert "&lt;b&gt;" in page and "--privacy SELF_ONLY --agree-music-terms" in page and "clip.mp4" in page
    q.approve(item["id"], "Owner", SETTINGS)
    assert "Music Usage Confirmation" in render_review_html(q.load())


def test_brief_text_constant_is_valid():
    assert parse_brief(BRIEF_TEXT).max_hashtags == 4

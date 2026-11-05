import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tt_poster.brief import parse_brief  # noqa: E402

BRIEF_TEXT = """topic: Three checks before you buy a standing desk
video: clip.mp4
max_chars: 300
max_hashtags: 4
ai_generated: no
facts:
- Our desks adjust from 62 cm to 127 cm.
- Frames have a 5 year warranty.
banned_claims: cheapest
---
Short and friendly.
"""
GOOD_CAPTION = "Three things to check before you buy a standing desk. Ours go from 62 cm to 127 cm."
LOG_ID = "202210112248442CB9319E1FB30C1073F3"
API = "https://open.tiktokapis.com"
UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=67890&upload_token=Xza123"


def ok(data):
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": LOG_ID}}


def err(code, message=""):
    return {"error": {"code": code, "message": message, "log_id": LOG_ID}}


CREATOR = {"creator_avatar_url": "https://lf16-tt4d.tiktokcdn.com/obj/tiktok-open-platform/x",
           "creator_username": "deskshop", "creator_nickname": "Desk Shop",
           "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
           "comment_disabled": False, "duet_disabled": False, "stitch_disabled": True,
           "max_video_post_duration_sec": 300}


@pytest.fixture
def brief():
    return parse_brief(BRIEF_TEXT, name="desk")


@pytest.fixture
def brief_file(tmp_path):
    p = tmp_path / "desk.md"
    p.write_text(BRIEF_TEXT)
    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 4000)
    return p


class FakeAnthropic:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=400, output_tokens=100),
                               content=[SimpleNamespace(type="text", text=json.dumps(self.replies.pop(0)))])

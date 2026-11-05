"""The approval queue. Every caption lands here as `pending`; nothing is posted unless a person
marks it `approved`. Approval is pinned to a hash of the caption AND the video file, so changing
either after approval drops it back to `pending`.

Approving also records the post settings TikTok's Content Sharing Guidelines say the user must
choose themselves: the privacy level (no default), whether comments, duets and stitches are
allowed (all off unless ticked), the commercial content disclosure, and consent to TikTok's
Music Usage Confirmation.

queue.json is the source of truth, written atomically. review.html is a read-only view of it.
"""
from __future__ import annotations

import html
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .draft import Caption

STATUSES = ("pending", "approved", "rejected", "published", "sent_to_inbox", "processing", "failed")
PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")


class QueueError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ApprovalQueue:
    def __init__(self, path: str | Path = "queue/queue.json"):
        self.path = Path(path)

    # storage
    def load(self) -> List[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8")).get("items", [])

    def save(self, items: List[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, item_id: str) -> dict:
        for item in self.load():
            if item["id"] == item_id:
                return item
        raise QueueError(f"no draft with id {item_id!r}")

    def _update(self, item_id: str, **changes) -> dict:
        items = self.load()
        for item in items:
            if item["id"] == item_id:
                item.update(changes)
                item["updated_at"] = now_iso()
                self.save(items)
                return item
        raise QueueError(f"no draft with id {item_id!r}")

    # actions
    def add(self, brief_name: str, draft: Caption, report_list: List[dict], ok: bool,
            video: str = "", video_digest: str = "") -> dict:
        items = self.load()
        item = {
            "id": secrets.token_hex(4),
            "brief": brief_name,
            "status": "pending" if ok else "failed",
            "draft": draft.to_dict(),
            "commentary": draft.title(),
            "video": video,
            "video_digest": video_digest,
            "content_hash": draft.content_hash(video_digest),
            "checks": report_list,
            "post_settings": None,
            "publish_id": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "approved_hash": None,
            "approved_by": None,
            "approved_at": None,
            "post_urn": None,
            "error": None if ok else "failed validation, fix the brief and draft again",
        }
        items.append(item)
        self.save(items)
        return item

    def approve(self, item_id: str, by: str, settings: dict) -> dict:
        item = self.get(item_id)
        if item["status"] != "pending":
            raise QueueError(f"draft {item_id} is {item['status']}, only pending drafts can be approved")
        if any(f["severity"] == "error" for f in item["checks"]):
            raise QueueError(f"draft {item_id} has failed checks and cannot be approved")
        if not by.strip():
            raise QueueError("say who is approving (--by)")
        check_settings(settings)
        return self._update(item_id, status="approved", approved_hash=item["content_hash"],
                            approved_by=by.strip(), approved_at=now_iso(), post_settings=settings)

    def reject(self, item_id: str, reason: str = "") -> dict:
        item = self.get(item_id)
        if item["status"] == "published":
            raise QueueError("this draft is already published; delete it in the TikTok app instead")
        return self._update(item_id, status="rejected", error=reason or None)

    def edit(self, item_id: str, draft: Caption, report_list: List[dict], ok: bool) -> dict:
        item = self.get(item_id)
        if item["status"] == "published":
            raise QueueError("this draft is already published")
        return self._update(item_id, draft=draft.to_dict(), commentary=draft.title(),
                            content_hash=draft.content_hash(item.get("video_digest", "")), checks=report_list,
                            status="pending" if ok else "failed",
                            approved_hash=None, approved_by=None, approved_at=None, post_settings=None)

    def mark_result(self, item_id: str, status: str, publish_id: str, post_ids: Optional[List] = None,
                    error: Optional[str] = None) -> dict:
        return self._update(item_id, status=status, publish_id=publish_id, post_urn=(post_ids or None),
                            finished_at=now_iso(), error=error)

    def mark_failed(self, item_id: str, error: str) -> dict:
        return self._update(item_id, status="failed", error=error)

    def approved(self) -> List[dict]:
        return [i for i in self.load() if i["status"] == "approved"]


def check_settings(s: dict) -> None:
    """The rules from TikTok's Content Sharing Guidelines that can be enforced before posting."""
    mode = s.get("mode", "direct")
    if mode not in ("direct", "inbox"):
        raise QueueError("mode must be direct or inbox")
    if mode == "direct":
        if s.get("privacy_level") not in PRIVACY_LEVELS:
            raise QueueError("choose a privacy level (--privacy); TikTok requires the user to pick it, no default")
        if not s.get("music_consent"):
            raise QueueError("posting needs your agreement to TikTok's Music Usage Confirmation (--agree-music-terms)")
        if s.get("branded_content") and s.get("privacy_level") == "SELF_ONLY":
            raise QueueError("branded content visibility cannot be set to private (TikTok rule)")
    if s.get("disclose_commercial") and not (s.get("your_brand") or s.get("branded_content")):
        raise QueueError("you need to indicate if your content promotes yourself, a third party, or both")


def consent_line(s: dict) -> str:
    if s.get("branded_content"):
        return "By posting, you agree to TikTok's Branded Content Policy and Music Usage Confirmation."
    return "By posting, you agree to TikTok's Music Usage Confirmation."


def render_review_html(items: List[dict], queue_path: str = "queue/queue.json") -> str:
    """A static, read-only review page. Approving is done with the CLI command shown on each card,
    so the page itself can never publish anything."""
    colours = {"pending": "#b26a00", "approved": "#1a7f37", "rejected": "#777", "published": "#fe2c55",
               "sent_to_inbox": "#25a3b8", "processing": "#6b4fbb",
               "failed": "#c62828"}
    cards = []
    order = {s: i for i, s in enumerate(STATUSES)}
    for item in sorted(items, key=lambda i: (order.get(i["status"], 9), i["created_at"])):
        esc = html.escape
        checks = "".join(
            f'<li class="{esc(c["severity"])}">{esc(c["severity"]).upper()}: {esc(c["message"])}</li>'
            for c in item["checks"]) or '<li class="ok">All checks passed.</li>'
        action = ""
        if item["status"] == "pending":
            action = (f'<pre>python -m tt_poster approve {esc(item["id"])} --by "your name" '
                      f'--privacy SELF_ONLY --agree-music-terms\n'
                      f'python -m tt_poster reject {esc(item["id"])} --reason "..."</pre>')
        elif item["status"] == "approved":
            ps = item.get("post_settings") or {}
            action = (f'<p class="meta">Approved by {esc(item["approved_by"])} at {esc(item["approved_at"])}: '
                      f'{esc(ps.get("mode", ""))} post, privacy {esc(str(ps.get("privacy_level")))}, '
                      f'comments {"on" if ps.get("allow_comment") else "off"}. {esc(consent_line(ps))}</p>')
        elif item["status"] in ("published", "sent_to_inbox", "processing"):
            action = f'<p class="meta">{esc(item["status"])}: publish_id {esc(item.get("publish_id") or "")}</p>'
        cards.append(f"""
<section class="card">
  <div class="top"><span class="badge" style="background:{colours.get(item['status'], '#555')}">
  {esc(item['status'])}</span> <b>{esc(item['id'])}</b> from <i>{esc(item['brief'])}</i>
  <span class="meta">{len(item['commentary'])} characters, created {esc(item['created_at'])}</span>
  <div class="meta">Video: {esc(Path(item.get('video') or '(none)').name)}</div></div>
  <div class="post">{esc(item['commentary'])}</div>
  <ul class="checks">{checks}</ul>
  {action}
</section>""")
    body = "".join(cards) or "<p>The queue is empty.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TikTok caption approval queue</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f1f1f2; margin:0; padding:24px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .sub {{ color:#555; margin-bottom:20px; }}
.card {{ background:#fff; border-radius:10px; padding:18px 20px; margin:0 auto 18px; max-width:640px;
        box-shadow:0 1px 3px rgba(0,0,0,.15); }}
.top {{ font-size:14px; margin-bottom:12px; }} .meta {{ color:#666; font-size:13px; }}
.badge {{ color:#fff; border-radius:12px; padding:2px 10px; font-size:12px; text-transform:uppercase; }}
.post {{ white-space:pre-wrap; font-size:15px; line-height:1.5; color:#191919; }}
.checks {{ font-size:13px; padding-left:18px; }} .error {{ color:#c62828; }} .warning {{ color:#b26a00; }}
.ok {{ color:#1a7f37; }} pre {{ background:#f6f8fa; padding:10px; border-radius:6px; font-size:12px; }}
</style></head><body>
<h1 style="max-width:640px;margin:0 auto 4px">TikTok caption approval queue</h1>
<p class="sub" style="max-width:640px;margin:0 auto 20px">Read-only view of {html.escape(queue_path)}.
Nothing here is posted until it is approved from the command line, with a privacy level chosen.</p>
{body}
</body></html>"""

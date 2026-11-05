"""Posts approved captions, and only approved captions, with their videos.

Gate before every post: status `approved`; caption AND video still match the approved hash;
checks pass again; not already in the posted log. Then, as TikTok's Content Sharing Guidelines
require: fresh creator_info, the creator must be able to post right now, the chosen privacy
level must be one the creator offers, interactions the creator has switched off stay off, and
the video must fit the creator's maximum duration. An app TikTok has not audited yet may only
post SELF_ONLY, so anything else is stopped here with a plain message instead of a 403.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import requests

from .brief import Brief
from .draft import Caption
from .queue import ApprovalQueue, now_iso
from .tiktok import CreatorInfo, TikTokClient, TikTokError
from .video import VideoError, check_limits, content_type, file_digest, plan_chunks, probe
from .validate import validate


@dataclass
class Outcome:
    item_id: str
    result: str          # published | sent_to_inbox | processing | dry_run | skipped_duplicate | blocked | failed
    detail: str = ""
    code: int = 0


@dataclass
class PublishRun:
    outcomes: List[Outcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return max([o.code or 1 for o in self.outcomes if o.result == "failed"], default=0)


class PostedLog:
    """Append-only JSON lines: id, hash, publish_id, status, privacy. No caption text, no tokens."""

    def __init__(self, path: str | Path = "queue/posted-log.jsonl"):
        self.path = Path(path)

    def hashes(self) -> set:
        if not self.path.exists():
            return set()
        return {json.loads(line)["content_hash"] for line in self.path.read_text().splitlines() if line.strip()}

    def append(self, **entry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now_iso(), **entry}) + "\n")


def build_post_info(cap: Caption, settings: dict, creator: Optional[CreatorInfo], ai_generated: bool) -> dict:
    """post_info for /v2/post/publish/video/init/. Interactions are off unless the approver allowed
    them AND the creator has not switched them off in TikTok."""
    def off(allowed_key: str, creator_disabled: bool) -> bool:
        return (not settings.get(allowed_key)) or creator_disabled

    return {
        "title": cap.title(),
        "privacy_level": settings["privacy_level"],
        "disable_duet": off("allow_duet", bool(creator and creator.duet_disabled)),
        "disable_comment": off("allow_comment", bool(creator and creator.comment_disabled)),
        "disable_stitch": off("allow_stitch", bool(creator and creator.stitch_disabled)),
        "video_cover_timestamp_ms": int(settings.get("cover_ms", 1000)),
        "brand_content_toggle": bool(settings.get("branded_content")),
        "brand_organic_toggle": bool(settings.get("your_brand")),
        "is_aigc": bool(ai_generated),
    }


def publish_approved(queue: ApprovalQueue, log: PostedLog, load_brief: Callable[[str], Brief],
                     client: Optional[TikTokClient], audited: bool = False, dry_run: bool = False,
                     only: Optional[str] = None, chunk_size: int = 10 * 1024 * 1024,
                     wait_seconds: float = 600, say: Callable[[str], None] = print,
                     prober: Optional[Callable] = None) -> PublishRun:
    run = PublishRun()
    items = [i for i in queue.approved() if only is None or i["id"] == only]
    if not items:
        say("Nothing approved to post.")
        return run
    posted = log.hashes()

    for item in items:
        iid = item["id"]
        settings = item.get("post_settings") or {}
        mode = settings.get("mode", "direct")
        cap = Caption.from_dict(item["draft"])

        def block(msg: str, status: str = "failed") -> None:
            queue._update(iid, status=status, error=msg)
            run.outcomes.append(Outcome(iid, "blocked", msg))

        video = item.get("video") or ""
        if not video or not Path(video).is_file():
            block(f"video file not found: {Path(video).name or '(none)'}")
            continue
        digest = file_digest(video)
        if cap.content_hash(digest) != item.get("approved_hash"):
            queue._update(iid, status="pending", approved_hash=None, approved_by=None, approved_at=None,
                          post_settings=None)
            run.outcomes.append(Outcome(iid, "blocked", "caption or video changed after approval, back to pending"))
            continue
        brief = load_brief(item["brief"])
        report = validate(cap, brief)
        if not report.ok:
            block("failed checks at post time: " + "; ".join(f.message for f in report.errors))
            continue
        if cap.content_hash(digest) in posted:
            queue._update(iid, status="rejected", error="duplicate of a video already posted")
            run.outcomes.append(Outcome(iid, "skipped_duplicate", "same caption and video already in the posted log"))
            continue
        if mode == "direct" and not audited and settings.get("privacy_level") != "SELF_ONLY":
            block("this app is not audited by TikTok yet, so it can only post SELF_ONLY; approve again with "
                  "--privacy SELF_ONLY, or set TIKTOK_APP_AUDITED=true once TikTok has approved the audit",
                  status="pending")
            queue._update(iid, approved_hash=None, approved_by=None, approved_at=None, post_settings=None)
            continue
        try:
            plan = plan_chunks(Path(video).stat().st_size, chunk_size)
            ctype = content_type(video)
            info = (prober or probe)(video)
            problems = check_limits(info) if info else []
        except VideoError as exc:
            block(str(exc))
            continue
        if problems:
            block("video does not meet TikTok's limits: " + "; ".join(problems))
            continue

        if dry_run:
            post_info = build_post_info(cap, settings, None, brief.ai_generated) if mode == "direct" else None
            body = {"source_info": TikTokClient.file_source(plan)}
            if post_info:
                body = {"post_info": post_info, **body}
            path = "/v2/post/publish/video/init/" if mode == "direct" else "/v2/post/publish/inbox/video/init/"
            say(f"[dry run] draft {iid}: POST {path}\n" + json.dumps(body, indent=2, ensure_ascii=False))
            for n, (a, b) in enumerate(plan.ranges(), 1):
                say(f"[dry run]   PUT chunk {n}/{plan.total_chunk_count}: Content-Type {ctype}, "
                    f"Content-Range bytes {a}-{b}/{plan.video_size}")
            run.outcomes.append(Outcome(iid, "dry_run", f"{plan.total_chunk_count} chunk(s), {plan.video_size} bytes"))
            continue

        if client is None:
            raise TikTokError("no TikTok client; run `auth` first")
        try:
            creator = client.creator_info()
            if mode == "direct":
                if settings["privacy_level"] not in creator.privacy_level_options:
                    block(f"privacy {settings['privacy_level']} is not offered to @{creator.username} "
                          f"(options: {', '.join(creator.privacy_level_options)})")
                    continue
                if info:
                    too_long = check_limits(info, creator.max_video_post_duration_sec)
                    if too_long:
                        block("; ".join(too_long))
                        continue
                say(f"Posting to @{creator.username} ({creator.nickname}) as {settings['privacy_level']}")
                init = client.init_direct_post(build_post_info(cap, settings, creator, brief.ai_generated),
                                               client.file_source(plan))
            else:
                say(f"Sending to @{creator.username}'s TikTok inbox as a draft")
                init = client.init_inbox(client.file_source(plan))
            publish_id = init["publish_id"]
            queue._update(iid, status="processing", publish_id=publish_id)
            client.upload(init["upload_url"], video, plan, ctype)
            final = client.wait_for_status(publish_id, timeout=wait_seconds,
                                           on_update=lambda st: say(f"  status: {st.get('status')}"))
        except requests.RequestException:
            msg = "network error mid-post; check TikTok before approving this again"
            queue.mark_failed(iid, msg)
            run.outcomes.append(Outcome(iid, "failed", msg, code=1))
            break
        except TikTokError as exc:
            queue.mark_failed(iid, str(exc))
            run.outcomes.append(Outcome(iid, "failed", str(exc), code=exc.exit_code))
            if exc.exit_code in (3, 4, 6, 7):     # auth, rate limit, posting blocked, unaudited: stop the batch
                break
            continue

        status = final.get("status")
        post_ids = final.get("publicaly_available_post_id") or []   # TikTok's own spelling
        if status == "PUBLISH_COMPLETE":
            result = "published"
        elif status == "SEND_TO_USER_INBOX":
            result = "sent_to_inbox"
        elif status == "FAILED":
            reason = final.get("fail_reason", "unknown")
            queue.mark_result(iid, "failed", publish_id, error=f"TikTok processing failed: {reason}")
            run.outcomes.append(Outcome(iid, "failed", f"TikTok processing failed: {reason}", code=8))
            continue
        else:
            result = "processing"      # still in moderation; `status <publish_id>` checks later
        queue.mark_result(iid, result, publish_id, post_ids)
        log.append(id=iid, content_hash=cap.content_hash(digest), publish_id=publish_id, status=status,
                   privacy=settings.get("privacy_level"), mode=mode, approved_by=item.get("approved_by"))
        posted.add(cap.content_hash(digest))
        detail = f"publish_id {publish_id}" + (f", post id {post_ids[0]}" if post_ids else "")
        if result == "sent_to_inbox":
            detail += "; open TikTok and tap the inbox notification to finish the post"
        run.outcomes.append(Outcome(iid, result, detail))
    for o in run.outcomes:
        say(f"{o.item_id}: {o.result} {o.detail}".rstrip())
    return run

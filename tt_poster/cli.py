"""Command line: auth, whoami, draft, review, list, approve, reject, edit, publish, status, revoke."""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path
from typing import List, Optional

from .brief import BriefError, load_brief
from .draft import Caption
from .drafter import ClaudeDrafter, OfflineDrafter, draft_with_checks
from .publisher import PostedLog, publish_approved
from .queue import ApprovalQueue, QueueError, consent_line, render_review_html
from .tiktok import (TikTokClient, TikTokError, authorization_url, exchange_code, new_state, parse_redirect,
                     refresh_token, revoke)
from .tokens import TokenStoreError, make_store, save_token_response, seconds_left
from .validate import validate
from .video import VideoError, file_digest

DEFAULT_REDIRECT = "https://parishsynthmind.github.io/tiktok-content-posting-client/callback/"


def _queue_dir() -> Path:
    return Path(os.environ.get("QUEUE_DIR", "queue"))


def _queue() -> ApprovalQueue:
    return ApprovalQueue(_queue_dir() / "queue.json")


def _write_review(queue: ApprovalQueue) -> Path:
    out = _queue_dir() / "review.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_review_html(queue.load(), str(queue.path)), encoding="utf-8")
    return out


def _redirect() -> str:
    return os.environ.get("TIKTOK_REDIRECT_URI", DEFAULT_REDIRECT)


def make_client(store) -> TikTokClient:
    """A client whose token is refreshed before it expires, and once more if TikTok says it is
    invalid. Every refresh saves the new (rotated) refresh token straight away."""
    def do_refresh() -> str:
        if seconds_left(store, "refresh_expires_at") <= 0:
            raise TikTokError("the TikTok refresh token has expired (they last about 180 days); run `auth`")
        data = refresh_token(store.get("refresh_token"), store.get("client_key"), store.get("client_secret"))
        save_token_response(store, data)
        return data["access_token"]

    token = do_refresh() if seconds_left(store) < 300 else store.get("access_token")
    return TikTokClient(token, refresh=do_refresh)


def cmd_auth(args) -> int:
    store = make_store(args.store)
    client_key, client_secret = store.get("client_key"), store.get("client_secret")
    state = new_state()
    url = authorization_url(client_key, _redirect(), state)
    print("1. Sign in to TikTok on this page and press Authorize:\n" + url)
    if not args.no_browser:
        webbrowser.open(url)
    redirected = args.redirected_url or input("2. Paste the full address of the page TikTok sent you to: ")
    code = parse_redirect(redirected, state)
    data = exchange_code(code, client_key, client_secret, _redirect())
    save_token_response(store, data)
    print(f"Signed in. Scopes granted: {data.get('scope', 'not reported')}. "
          f"Access token lasts {int(data['expires_in']) // 3600} h and refreshes itself; "
          f"sign in again within {int(data['refresh_expires_in']) // 86400} days.")
    return 0


def cmd_whoami(args) -> int:
    store = make_store(args.store)
    info = make_client(store).creator_info()
    print(f"Posting as @{info.username} ({info.nickname})")
    print(f"Privacy options: {', '.join(info.privacy_level_options)}")
    print(f"Comments {'off' if info.comment_disabled else 'allowed'}, "
          f"duets {'off' if info.duet_disabled else 'allowed'}, "
          f"stitches {'off' if info.stitch_disabled else 'allowed'}; max video {info.max_video_post_duration_sec}s")
    print(f"Sign in again within {seconds_left(store, 'refresh_expires_at') / 86400:.0f} days")
    return 0


def cmd_draft(args) -> int:
    brief = load_brief(args.brief)
    video = args.video or brief.video
    if not video or not Path(video).is_file():
        raise VideoError(f"video file not found: {video or '(set video: in the brief or pass --video)'}")
    drafter = OfflineDrafter() if args.offline else ClaudeDrafter(model=args.model)
    cap, report, attempts = draft_with_checks(drafter, brief, attempts=args.attempts)
    queue = _queue()
    item = queue.add(str(args.brief), cap, report.to_list(), report.ok, video=str(Path(video).resolve()),
                     video_digest=file_digest(video))
    print(f"Caption {item['id']} ({len(cap.title())} characters, {attempts} attempt(s)): {item['status']}")
    print(cap.title())
    print(report.as_text())
    if getattr(drafter, "usage", None):
        print(f"Claude tokens: {sum(u[0] for u in drafter.usage)} in, {sum(u[1] for u in drafter.usage)} out")
    print(f"Review page: {_write_review(queue)}")
    return 0 if report.ok else 2


def cmd_review(args) -> int:
    out = _write_review(_queue())
    print(f"Review page: {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def cmd_list(args) -> int:
    for item in _queue().load():
        print(f"{item['id']}  {item['status']:<13}  {len(item['commentary']):>4} chars  {item['commentary'][:60]}")
    return 0


def cmd_approve(args) -> int:
    settings = {
        "mode": args.mode, "privacy_level": args.privacy, "music_consent": args.agree_music_terms,
        "allow_comment": args.allow_comment, "allow_duet": args.allow_duet, "allow_stitch": args.allow_stitch,
        "disclose_commercial": args.your_brand or args.branded_content,
        "your_brand": args.your_brand, "branded_content": args.branded_content, "cover_ms": args.cover_ms,
    }
    item = _queue().approve(args.id, args.by, settings)
    _write_review(_queue())
    print(f"Approved {item['id']} as a {args.mode} post"
          + (f", privacy {args.privacy}." if args.mode == "direct" else " (lands in your TikTok inbox)."))
    if args.mode == "direct":
        print(consent_line(settings))
    return 0


def cmd_reject(args) -> int:
    _queue().reject(args.id, args.reason)
    _write_review(_queue())
    print(f"Rejected {args.id}.")
    return 0


def cmd_edit(args) -> int:
    queue = _queue()
    item = queue.get(args.id)
    old = Caption.from_dict(item["draft"])
    text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else old.text
    tags = [t.strip().lstrip("#") for t in args.hashtags.split(",") if t.strip()] if args.hashtags is not None \
        else old.hashtags
    cap = Caption(text=text.strip(), hashtags=tags, facts_used=old.facts_used)
    report = validate(cap, load_brief(item["brief"]))
    queue.edit(args.id, cap, report.to_list(), report.ok)
    _write_review(queue)
    print(f"Edited {args.id}; it is back to {'pending' if report.ok else 'failed'} and needs approving again.")
    print(report.as_text())
    return 0 if report.ok else 2


def cmd_publish(args) -> int:
    queue = _queue()
    client = None if args.dry_run else make_client(make_store(args.store))
    audited = os.environ.get("TIKTOK_APP_AUDITED", "false").lower() == "true"
    run = publish_approved(queue, PostedLog(_queue_dir() / "posted-log.jsonl"), load_brief, client,
                           audited=audited, dry_run=args.dry_run, only=args.id, wait_seconds=args.wait)
    _write_review(queue)
    return run.exit_code


def cmd_status(args) -> int:
    st = make_client(make_store(args.store)).fetch_status(args.publish_id)
    print(f"{args.publish_id}: {st.get('status')}"
          + (f" ({st['fail_reason']})" if st.get("fail_reason") else "")
          + (f", post id {st['publicaly_available_post_id'][0]}" if st.get("publicaly_available_post_id") else ""))
    return 0


def cmd_revoke(args) -> int:
    store = make_store(args.store)
    revoke(store.get("access_token"), store.get("client_key"), store.get("client_secret"))
    print("Access revoked. Run `auth` to sign in again.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tt_poster", description=__doc__)
    p.add_argument("--store", default="", help="keychain (default) or file")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="sign in with TikTok (Login Kit) and save the tokens")
    a.add_argument("--no-browser", action="store_true")
    a.add_argument("--redirected-url", help="the address TikTok redirected to (otherwise asked for)")
    a.set_defaults(func=cmd_auth)

    sub.add_parser("whoami", help="creator info: who you post as and what is allowed").set_defaults(func=cmd_whoami)

    d = sub.add_parser("draft", help="write a caption for a video into the approval queue")
    d.add_argument("brief")
    d.add_argument("--video", help="video file (overrides video: in the brief)")
    d.add_argument("--offline", action="store_true", help="no AI, template caption")
    d.add_argument("--attempts", type=int, default=2)
    d.add_argument("--model", default="claude-sonnet-5-5")
    d.set_defaults(func=cmd_draft)

    r = sub.add_parser("review", help="rebuild queue/review.html")
    r.add_argument("--open", action="store_true")
    r.set_defaults(func=cmd_review)

    sub.add_parser("list", help="list the queue").set_defaults(func=cmd_list)

    ap = sub.add_parser("approve", help="approve a caption and choose how it is posted")
    ap.add_argument("id")
    ap.add_argument("--by", required=True)
    ap.add_argument("--mode", choices=["direct", "inbox"], default="direct",
                    help="direct = post now; inbox = send to your TikTok inbox to finish in the app")
    ap.add_argument("--privacy", choices=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR",
                                          "SELF_ONLY"], help="required for direct posts, no default")
    ap.add_argument("--agree-music-terms", action="store_true",
                    help="you agree to TikTok's Music Usage Confirmation (required for direct posts)")
    ap.add_argument("--allow-comment", action="store_true")
    ap.add_argument("--allow-duet", action="store_true")
    ap.add_argument("--allow-stitch", action="store_true")
    ap.add_argument("--your-brand", action="store_true", help="promotes your own business")
    ap.add_argument("--branded-content", action="store_true", help="paid partnership with a third party")
    ap.add_argument("--cover-ms", type=int, default=1000, help="cover frame, milliseconds into the video")
    ap.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="reject a caption")
    rj.add_argument("id")
    rj.add_argument("--reason", default="")
    rj.set_defaults(func=cmd_reject)

    e = sub.add_parser("edit", help="replace a caption's text or hashtags (needs approving again)")
    e.add_argument("id")
    e.add_argument("--text-file")
    e.add_argument("--hashtags", help="comma separated, without #")
    e.set_defaults(func=cmd_edit)

    pb = sub.add_parser("publish", help="post every approved caption and video")
    pb.add_argument("--dry-run", action="store_true", help="show the exact requests, send nothing")
    pb.add_argument("--id", help="post only this one")
    pb.add_argument("--wait", type=float, default=600, help="seconds to wait for TikTok processing")
    pb.set_defaults(func=cmd_publish)

    st = sub.add_parser("status", help="check a publish_id")
    st.add_argument("publish_id")
    st.set_defaults(func=cmd_status)

    sub.add_parser("revoke", help="revoke this app's access to the account").set_defaults(func=cmd_revoke)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (BriefError, QueueError, TokenStoreError, VideoError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except TikTokError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code

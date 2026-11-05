import json
import time

import pytest
import requests
import responses

from tt_poster import cli, publisher
from tt_poster.brief import load_brief
from tt_poster.draft import Caption
from tt_poster.drafter import ClaudeDrafter
from tt_poster.queue import ApprovalQueue
from tt_poster.tiktok import TikTokError
from tt_poster.validate import validate
from tt_poster.video import VideoInfo, file_digest

from .conftest import API, BRIEF_TEXT, CREATOR, GOOD_CAPTION, UPLOAD_URL, FakeAnthropic, err, ok

SELF = ["--privacy", "SELF_ONLY", "--agree-music-terms"]


@pytest.fixture
def env(tmp_path, monkeypatch, brief_file):
    monkeypatch.setenv("QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setenv("TIKTOK_TOKEN_STORE", "file")
    monkeypatch.setenv("TIKTOK_TOKEN_FILE", str(tmp_path / "tok.json"))
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    monkeypatch.setenv("TIKTOK_REDIRECT_URI", "https://example.github.io/cb/")
    monkeypatch.delenv("TIKTOK_APP_AUDITED", raising=False)
    monkeypatch.setattr(publisher, "probe", lambda p: VideoInfo(14.5, 1080, 1920, 30.0, "h264"))
    now = int(time.time())
    (tmp_path / "tok.json").write_text(json.dumps({
        "access_token": "act.valid", "refresh_token": "rft.one", "open_id": "oid",
        "expires_at": str(now + 3600), "refresh_expires_at": str(now + 86400 * 100)}))
    return tmp_path


def queued(env, brief_file, approve=SELF, text=GOOD_CAPTION):
    q = ApprovalQueue(env / "queue" / "queue.json")
    brief = load_brief(brief_file)
    cap = Caption(text, ["standingdesk"])
    r = validate(cap, brief)
    item = q.add(str(brief_file), cap, r.to_list(), r.ok, video=brief.video, video_digest=file_digest(brief.video))
    if approve is not None:
        assert cli.main(["approve", item["id"], "--by", "Owner", *approve]) == 0
    return q, item


def mock_happy_path(status="PUBLISH_COMPLETE"):
    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    responses.post(API + "/v2/post/publish/video/init/",
                   json=ok({"publish_id": "v_pub_file~v2-1.123456789", "upload_url": UPLOAD_URL}))
    responses.put(UPLOAD_URL, status=201)
    responses.post(API + "/v2/post/publish/status/fetch/", json=ok({"status": "PROCESSING_UPLOAD"}))
    responses.post(API + "/v2/post/publish/status/fetch/", json=ok({"status": status,
                                                                     "publicaly_available_post_id": []}))


def posts(path):
    return [c for c in responses.calls if c.request.url.endswith(path)]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@responses.activate
def test_pending_is_never_posted(env, brief_file):
    queued(env, brief_file, approve=None)
    assert cli.main(["publish"]) == 0
    assert not responses.calls


@responses.activate
def test_full_direct_post_then_duplicate_guard(env, brief_file, capsys):
    mock_happy_path()
    q, item = queued(env, brief_file)
    assert cli.main(["publish"]) == 0
    got = q.get(item["id"])
    assert got["status"] == "published" and got["publish_id"] == "v_pub_file~v2-1.123456789"
    body = json.loads(posts("/video/init/")[0].request.body)
    assert body["post_info"] == {
        "title": GOOD_CAPTION + " #standingdesk", "privacy_level": "SELF_ONLY",
        "disable_duet": True, "disable_comment": True, "disable_stitch": True,   # off unless allowed
        "video_cover_timestamp_ms": 1000, "brand_content_toggle": False, "brand_organic_toggle": False,
        "is_aigc": False}
    assert body["source_info"]["source"] == "FILE_UPLOAD" and body["source_info"]["total_chunk_count"] == 1
    out = capsys.readouterr().out
    assert "Posting to @deskshop (Desk Shop) as SELF_ONLY" in out and "act.valid" not in out
    log = (env / "queue" / "posted-log.jsonl").read_text()
    assert "v_pub_file" in log and "standing desk" not in log.lower() and "act.valid" not in log

    q2, item2 = queued(env, brief_file)
    assert cli.main(["publish"]) == 0
    assert q2.get(item2["id"])["status"] == "rejected"
    assert len(posts("/video/init/")) == 1


@responses.activate
def test_allowed_interactions_but_creator_disabled_stitch(env, brief_file):
    mock_happy_path()
    queued(env, brief_file, approve=[*SELF, "--allow-comment", "--allow-duet", "--allow-stitch"])
    assert cli.main(["publish"]) == 0
    pi = json.loads(posts("/video/init/")[0].request.body)["post_info"]
    assert (pi["disable_comment"], pi["disable_duet"], pi["disable_stitch"]) == (False, False, True)


@responses.activate
def test_unaudited_app_blocks_public_before_any_call(env, brief_file):
    q, item = queued(env, brief_file, approve=["--privacy", "PUBLIC_TO_EVERYONE", "--agree-music-terms"])
    assert cli.main(["publish"]) == 0
    got = q.get(item["id"])
    assert got["status"] == "pending" and "not audited" in got["error"] and got["post_settings"] is None
    assert not responses.calls


@responses.activate
def test_audited_app_can_post_public(env, brief_file, monkeypatch):
    monkeypatch.setenv("TIKTOK_APP_AUDITED", "true")
    mock_happy_path()
    queued(env, brief_file, approve=["--privacy", "PUBLIC_TO_EVERYONE", "--agree-music-terms", "--your-brand"])
    assert cli.main(["publish"]) == 0
    pi = json.loads(posts("/video/init/")[0].request.body)["post_info"]
    assert pi["privacy_level"] == "PUBLIC_TO_EVERYONE" and pi["brand_organic_toggle"] is True


@responses.activate
def test_privacy_not_offered_by_creator(env, brief_file, monkeypatch):
    monkeypatch.setenv("TIKTOK_APP_AUDITED", "true")
    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    q, item = queued(env, brief_file, approve=["--privacy", "FOLLOWER_OF_CREATOR", "--agree-music-terms"])
    assert cli.main(["publish"]) == 0
    assert "not offered" in q.get(item["id"])["error"] and not posts("/video/init/")


@responses.activate
def test_video_too_long_for_creator(env, brief_file, monkeypatch):
    monkeypatch.setattr(publisher, "probe", lambda p: VideoInfo(400, 1080, 1920, 30.0, "h264"))
    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    q, item = queued(env, brief_file)
    cli.main(["publish"])
    assert "up to 300s" in q.get(item["id"])["error"] and not posts("/video/init/")


@responses.activate
def test_bad_video_format_blocked(env, brief_file, monkeypatch):
    monkeypatch.setattr(publisher, "probe", lambda p: VideoInfo(10, 200, 200, 12, "h264"))
    q, item = queued(env, brief_file)
    cli.main(["publish"])
    assert "frame rate" in q.get(item["id"])["error"] and not responses.calls


@responses.activate
def test_creator_cannot_post_now(env, brief_file):
    responses.post(API + "/v2/post/publish/creator_info/query/", json=err("spam_risk_too_many_posts"))
    q, item = queued(env, brief_file)
    assert cli.main(["publish"]) == 6
    assert q.get(item["id"])["status"] == "failed"


@responses.activate
def test_inbox_mode(env, brief_file, capsys):
    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    responses.post(API + "/v2/post/publish/inbox/video/init/",
                   json=ok({"publish_id": "v_inbox_file~v2.123456789", "upload_url": UPLOAD_URL}))
    responses.put(UPLOAD_URL, status=201)
    responses.post(API + "/v2/post/publish/status/fetch/", json=ok({"status": "SEND_TO_USER_INBOX"}))
    q, item = queued(env, brief_file, approve=["--mode", "inbox"])
    assert cli.main(["publish"]) == 0
    assert q.get(item["id"])["status"] == "sent_to_inbox"
    assert "tap the inbox notification" in capsys.readouterr().out


@responses.activate
def test_processing_failed(env, brief_file):
    mock_happy_path(status="FAILED")
    q, item = queued(env, brief_file)
    assert cli.main(["publish"]) == 8
    assert "TikTok processing failed" in q.get(item["id"])["error"]


@responses.activate
def test_still_in_moderation_is_processing(env, brief_file):
    mock_happy_path(status="PROCESSING_DOWNLOAD")
    q, item = queued(env, brief_file)
    assert cli.main(["publish", "--wait", "0"]) == 0
    assert q.get(item["id"])["status"] == "processing"


@responses.activate
def test_upload_url_expired(env, brief_file):
    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    responses.post(API + "/v2/post/publish/video/init/",
                   json=ok({"publish_id": "v_pub_file~v2-1.1", "upload_url": UPLOAD_URL}))
    responses.put(UPLOAD_URL, status=403)
    q, item = queued(env, brief_file)
    assert cli.main(["publish"]) == 8
    assert "expired" in q.get(item["id"])["error"]


@responses.activate
def test_network_error_not_retried_blindly(env, brief_file):
    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    responses.post(API + "/v2/post/publish/video/init/", body=requests.ConnectionError("reset"))
    q, item = queued(env, brief_file)
    assert cli.main(["publish"]) == 1
    assert "check TikTok" in q.get(item["id"])["error"] and len(posts("/video/init/")) == 1


@responses.activate
def test_video_changed_after_approval(env, brief_file):
    q, item = queued(env, brief_file)
    (env / "clip.mp4").write_bytes(b"different video")
    assert cli.main(["publish"]) == 0
    assert q.get(item["id"])["status"] == "pending" and not responses.calls


def test_missing_video_blocked(env, brief_file):
    q, item = queued(env, brief_file)
    (env / "clip.mp4").unlink()
    cli.main(["publish", "--dry-run"])
    assert "not found" in q.get(item["id"])["error"]


def test_caption_fails_checks_at_post_time(env, brief_file):
    q, item = queued(env, brief_file)
    brief_file.write_text(BRIEF_TEXT.replace("62 cm", "60 cm"))
    cli.main(["publish", "--dry-run"])
    assert q.get(item["id"])["status"] == "failed"


@responses.activate
def test_dry_run_prints_requests_and_sends_nothing(env, brief_file, capsys):
    queued(env, brief_file)
    assert cli.main(["publish", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry run]" in out and '"privacy_level": "SELF_ONLY"' in out and "Content-Range bytes 0-" in out
    assert not responses.calls


@responses.activate
def test_expired_access_token_is_refreshed_and_rotated_token_saved(env, brief_file):
    tok = json.loads((env / "tok.json").read_text())
    tok["expires_at"] = "100"
    (env / "tok.json").write_text(json.dumps(tok))
    responses.post(API + "/v2/oauth/token/", json={
        "access_token": "act.new", "expires_in": 86400, "open_id": "oid", "refresh_expires_in": 31536000,
        "refresh_token": "rft.two", "scope": "user.info.basic,video.publish", "token_type": "Bearer"})
    mock_happy_path()
    queued(env, brief_file)
    assert cli.main(["publish"]) == 0
    saved = json.loads((env / "tok.json").read_text())
    assert saved["refresh_token"] == "rft.two" and saved["access_token"] == "act.new"
    assert posts("/creator_info/query/")[0].request.headers["Authorization"] == "Bearer act.new"


def test_refresh_token_expired_needs_auth(env, brief_file):
    tok = json.loads((env / "tok.json").read_text())
    tok.update(expires_at="100", refresh_expires_at="100")
    (env / "tok.json").write_text(json.dumps(tok))
    queued(env, brief_file)
    assert cli.main(["publish"]) == 1


def test_live_publish_without_client(env, brief_file):
    q, _ = queued(env, brief_file)
    with pytest.raises(TikTokError):
        publisher.publish_approved(q, publisher.PostedLog(env / "l.jsonl"), load_brief, None, say=lambda s: None)


def test_cli_draft_edit_reject_list(env, brief_file, monkeypatch, capsys, tmp_path):
    fake = FakeAnthropic([{"text": GOOD_CAPTION, "hashtags": ["standingdesk"], "facts_used": [0]}])
    monkeypatch.setattr(cli, "ClaudeDrafter", lambda model: ClaudeDrafter(model=model, client=fake))
    assert cli.main(["draft", str(brief_file)]) == 0
    assert "Claude tokens: 400 in, 100 out" in capsys.readouterr().out
    assert cli.main(["draft", str(brief_file), "--offline"]) == 0
    q = ApprovalQueue(env / "queue" / "queue.json")
    iid = q.load()[0]["id"]
    assert cli.main(["list"]) == 0 and iid in capsys.readouterr().out
    assert cli.main(["approve", iid, "--by", "Owner"]) == 2          # no privacy chosen
    bad = tmp_path / "bad.txt"
    bad.write_text(GOOD_CAPTION + " 99% love it")
    assert cli.main(["edit", iid, "--text-file", str(bad)]) == 2
    assert cli.main(["edit", iid, "--text-file", str(bad), "--hashtags", "desk,office"]) == 2
    assert cli.main(["reject", iid, "--reason", "no"]) == 0
    assert cli.main(["review"]) == 0
    assert cli.main(["draft", str(brief_file), "--video", str(env / "missing.mp4")]) == 2


@responses.activate
def test_cli_auth_whoami_status_revoke(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "new_state", lambda: "st8")
    responses.post(API + "/v2/oauth/token/", json={
        "access_token": "act.signed", "expires_in": 86400, "open_id": "oid2", "refresh_expires_in": 15552000,
        "refresh_token": "rft.signed", "scope": "user.info.basic,video.publish,video.upload", "token_type": "Bearer"})
    url = "https://example.github.io/cb/?code=CODE1&scopes=user.info.basic%2Cvideo.publish&state=st8"
    assert cli.main(["auth", "--no-browser", "--redirected-url", url]) == 0
    out = capsys.readouterr().out
    assert "180 days" in out and "act.signed" not in out and "client_key=ck" in out
    assert json.loads((env / "tok.json").read_text())["refresh_token"] == "rft.signed"
    assert cli.main(["auth", "--no-browser", "--redirected-url", url.replace("st8", "evil")]) == 3

    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    assert cli.main(["whoami"]) == 0
    assert "@deskshop" in capsys.readouterr().out
    responses.post(API + "/v2/post/publish/status/fetch/",
                   json=ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000000000000001]}))
    assert cli.main(["status", "v_pub_file~v2-1.1"]) == 0
    assert "7300000000000000001" in capsys.readouterr().out
    responses.post(API + "/v2/oauth/revoke/", json={})
    assert cli.main(["revoke"]) == 0

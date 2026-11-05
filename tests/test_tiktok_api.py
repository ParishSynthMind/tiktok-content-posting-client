"""TikTok HTTP is mocked with `responses`, using the request and response shapes from
developers.tiktok.com (Login Kit, token management, Content Posting API, Media Transfer Guide)."""
import json
import urllib.parse

import pytest
import requests
import responses

from tt_poster.tiktok import (AUTH_URL, AuthError, BadRequest, PostingBlocked, PrivacyMismatch, RateLimited,
                              ScopeError, TikTokClient, TikTokError, UnauditedApp, UploadError, authorization_url,
                              exchange_code, parse_redirect, refresh_token, revoke)
from tt_poster.video import plan_chunks

from .conftest import API, CREATOR, LOG_ID, UPLOAD_URL, err, ok

TOKEN = "act.example12345Example12345Example"
TOKEN_RESPONSE = {"access_token": TOKEN, "expires_in": 86400, "open_id": "asdf-12345c-1a2s3d-ac98-asdf123as12as34",
                  "refresh_expires_in": 31536000, "refresh_token": "rft.example12345Example12345Example",
                  "scope": "user.info.basic,video.publish,video.upload", "token_type": "Bearer"}


def client(**kw):
    sleeps = []
    return TikTokClient(TOKEN, sleep=sleeps.append, **kw), sleeps


# ---------- OAuth ----------

def test_authorization_url():
    url = authorization_url("ck123", "https://example.github.io/cb/", "st")
    base, _, query = url.partition("?")
    assert base == AUTH_URL == "https://www.tiktok.com/v2/auth/authorize/"
    assert dict(urllib.parse.parse_qsl(query)) == {
        "client_key": "ck123", "response_type": "code", "scope": "user.info.basic,video.publish,video.upload",
        "redirect_uri": "https://example.github.io/cb/", "state": "st"}


def test_parse_redirect():
    url = "https://example.github.io/cb/?code=abc%2A123&scopes=user.info.basic%2Cvideo.publish&state=st"
    assert parse_redirect(url, "st") == "abc*123"       # URL-decoded, as the token call requires
    with pytest.raises(AuthError, match="state"):
        parse_redirect(url, "other")
    with pytest.raises(AuthError, match="access_denied"):
        parse_redirect("https://x/cb/?error=access_denied&error_description=user+said+no&state=st", "st")
    with pytest.raises(AuthError, match="no code"):
        parse_redirect("https://x/cb/?state=st", "st")
    with pytest.raises(ScopeError):
        parse_redirect("https://x/cb/?code=c&scopes=user.info.basic&state=st", "st")


@responses.activate
def test_exchange_code_and_refresh_are_form_posts():
    responses.post(API + "/v2/oauth/token/", json=TOKEN_RESPONSE)
    responses.post(API + "/v2/oauth/token/", json={**TOKEN_RESPONSE, "refresh_token": "rft.new"})
    assert exchange_code("abc*123", "ck", "cs", "https://x/cb/")["open_id"].startswith("asdf")
    assert refresh_token("rft.old", "ck", "cs")["refresh_token"] == "rft.new"
    first, second = (dict(urllib.parse.parse_qsl(c.request.body)) for c in responses.calls)
    assert first == {"client_key": "ck", "client_secret": "cs", "code": "abc*123",
                     "grant_type": "authorization_code", "redirect_uri": "https://x/cb/"}
    assert second == {"client_key": "ck", "client_secret": "cs", "grant_type": "refresh_token",
                      "refresh_token": "rft.old"}
    h = responses.calls[0].request.headers
    assert h["Content-Type"] == "application/x-www-form-urlencoded" and h["Cache-Control"] == "no-cache"


@responses.activate
def test_token_error_shape_and_no_secret_in_message():
    responses.post(API + "/v2/oauth/token/", status=400, json={
        "error": "invalid_grant", "error_description": "Authorization code is expired.",
        "log_id": "202206221854370101130062072500FFA2"})
    with pytest.raises(AuthError) as e:
        exchange_code("c", "ck", "topsecret", "https://x/cb/")
    assert "invalid_grant" in str(e.value) and "topsecret" not in str(e.value) and e.value.log_id


@responses.activate
def test_revoke():
    responses.post(API + "/v2/oauth/revoke/", json={})
    revoke(TOKEN, "ck", "cs")
    assert dict(urllib.parse.parse_qsl(responses.calls[0].request.body))["token"] == TOKEN
    responses.post(API + "/v2/oauth/revoke/", status=500)
    with pytest.raises(AuthError):
        revoke(TOKEN, "ck", "cs")


# ---------- creator_info ----------

@responses.activate
def test_creator_info_request_and_shape():
    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    info = client()[0].creator_info()
    assert info.username == "deskshop" and info.stitch_disabled and info.max_video_post_duration_sec == 300
    assert info.privacy_level_options == ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"]
    req = responses.calls[0].request
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.headers["Content-Type"] == "application/json; charset=UTF-8"
    assert req.body is None


@responses.activate
def test_creator_info_spam_errors_come_back_as_200():
    responses.post(API + "/v2/post/publish/creator_info/query/", status=200, json=err("spam_risk_too_many_posts"))
    with pytest.raises(PostingBlocked, match="daily posting cap") as e:
        client()[0].creator_info()
    assert e.value.log_id == LOG_ID


# ---------- init ----------

POST_INFO = {"title": "this will be a funny #cat video", "privacy_level": "SELF_ONLY", "disable_duet": False,
             "disable_comment": True, "disable_stitch": False, "video_cover_timestamp_ms": 1000}


@responses.activate
def test_direct_post_init_body_and_response():
    responses.post(API + "/v2/post/publish/video/init/", json=ok({"publish_id": "v_pub_file~v2-1.123456789",
                                                                  "upload_url": UPLOAD_URL}))
    c, _ = client()
    plan = plan_chunks(50_000_123, 10_000_000)
    data = c.init_direct_post(POST_INFO, c.file_source(plan))
    assert data == {"publish_id": "v_pub_file~v2-1.123456789", "upload_url": UPLOAD_URL}
    assert json.loads(responses.calls[0].request.body) == {
        "post_info": POST_INFO,
        "source_info": {"source": "FILE_UPLOAD", "video_size": 50000123, "chunk_size": 10000000,
                        "total_chunk_count": 5}}


def test_direct_post_needs_explicit_privacy():
    c, _ = client()
    with pytest.raises(BadRequest, match="no default"):
        c.init_direct_post({"title": "x"}, {})


@responses.activate
def test_inbox_init_has_no_post_info():
    responses.post(API + "/v2/post/publish/inbox/video/init/",
                   json=ok({"publish_id": "v_inbox_file~v2.123456789", "upload_url": UPLOAD_URL}))
    c, _ = client()
    c.init_inbox(c.file_source(plan_chunks(4000)))
    assert json.loads(responses.calls[0].request.body) == {
        "source_info": {"source": "FILE_UPLOAD", "video_size": 4000, "chunk_size": 4000, "total_chunk_count": 1}}


@pytest.mark.parametrize("status,code,exc", [
    (403, "unaudited_client_can_only_post_to_private_accounts", UnauditedApp),
    (403, "privacy_level_option_mismatch", PrivacyMismatch),
    (403, "spam_risk_user_banned_from_posting", PostingBlocked),
    (403, "reached_active_user_cap", PostingBlocked),
    (403, "url_ownership_unverified", BadRequest),
    (400, "invalid_param", BadRequest),
    (401, "scope_not_authorized", ScopeError),
    (401, "access_token_invalid", AuthError),
])
@responses.activate
def test_init_error_codes(status, code, exc):
    responses.post(API + "/v2/post/publish/video/init/", status=status, json=err(code, "msg"))
    with pytest.raises(exc) as e:
        client()[0].init_direct_post(POST_INFO, {})
    assert e.value.code == code and TOKEN not in str(e.value)


@responses.activate
def test_unaudited_message_is_plain_english():
    responses.post(API + "/v2/post/publish/video/init/", status=403,
                   json=err("unaudited_client_can_only_post_to_private_accounts"))
    with pytest.raises(UnauditedApp, match="SELF_ONLY"):
        client()[0].init_direct_post(POST_INFO, {})


@responses.activate
def test_rate_limit_retries_then_gives_up():
    for _ in range(4):
        responses.post(API + "/v2/post/publish/video/init/", status=429, json=err("rate_limit_exceeded"))
    c, sleeps = client()
    with pytest.raises(RateLimited):
        c.init_direct_post(POST_INFO, {})
    assert len(sleeps) == 3 and len(responses.calls) == 4


@responses.activate
def test_5xx_retried_then_ok():
    responses.post(API + "/v2/post/publish/status/fetch/", status=503, body="busy")
    responses.post(API + "/v2/post/publish/status/fetch/", json=ok({"status": "PROCESSING_UPLOAD"}))
    assert client()[0].fetch_status("p")["status"] == "PROCESSING_UPLOAD"


@responses.activate
def test_expired_token_refreshes_once():
    responses.post(API + "/v2/post/publish/creator_info/query/", status=401, json=err("access_token_invalid"))
    responses.post(API + "/v2/post/publish/creator_info/query/", json=ok(CREATOR))
    calls = []
    c, _ = client(refresh=lambda: calls.append(1) or "act.fresh")
    assert c.creator_info().username == "deskshop" and calls == [1]
    assert responses.calls[1].request.headers["Authorization"] == "Bearer act.fresh"


@responses.activate
def test_refresh_only_tried_once():
    for _ in range(2):
        responses.post(API + "/v2/post/publish/creator_info/query/", status=401, json=err("access_token_invalid"))
    c, _ = client(refresh=lambda: "act.fresh")
    with pytest.raises(AuthError):
        c.creator_info()


@responses.activate
def test_non_json_error():
    responses.post(API + "/v2/post/publish/cancel/", status=404, body="not found")
    with pytest.raises(TikTokError, match="http_404"):
        client()[0].cancel("p")


# ---------- media transfer ----------

def _video(tmp_path, size):
    p = tmp_path / "v.mp4"
    with open(p, "wb") as fh:
        fh.write(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
    return p


@responses.activate
def test_chunked_upload_headers_and_bytes(tmp_path):
    size = 12 * 1024 * 1024 + 123
    path = _video(tmp_path, size)
    plan = plan_chunks(size, 5 * 1024 * 1024)          # 2 chunks, the last one holds the remainder
    responses.put(UPLOAD_URL, status=206)
    responses.put(UPLOAD_URL, status=201)
    assert client()[0].upload(UPLOAD_URL, path, plan, "video/mp4") == 2
    first, last = (c.request for c in responses.calls)
    assert first.url == UPLOAD_URL      # full upload_url, query string included
    assert first.headers["Content-Range"] == f"bytes 0-5242879/{size}"
    assert first.headers["Content-Length"] == "5242880" and first.headers["Content-Type"] == "video/mp4"
    assert last.headers["Content-Range"] == f"bytes 5242880-{size - 1}/{size}"
    assert len(first.body) + len(last.body) == size
    assert first.body + last.body == path.read_bytes()
    assert "Authorization" not in first.headers


@responses.activate
def test_single_chunk_upload(tmp_path):
    path = _video(tmp_path, 4000)
    responses.put(UPLOAD_URL, status=201)
    client()[0].upload(UPLOAD_URL, path, plan_chunks(4000), "video/mp4")
    assert responses.calls[0].request.headers["Content-Range"] == "bytes 0-3999/4000"


@pytest.mark.parametrize("status,match", [(403, "expired"), (416, "Content-Range"), (404, "no upload task"),
                                          (400, "headers")])
@responses.activate
def test_upload_errors(tmp_path, status, match):
    responses.put(UPLOAD_URL, status=status)
    with pytest.raises(UploadError, match=match):
        client()[0].upload(UPLOAD_URL, _video(tmp_path, 4000), plan_chunks(4000), "video/mp4")


@responses.activate
def test_upload_5xx_retries_same_chunk(tmp_path):
    responses.put(UPLOAD_URL, status=502)
    responses.put(UPLOAD_URL, status=201)
    c, sleeps = client()
    assert c.upload(UPLOAD_URL, _video(tmp_path, 4000), plan_chunks(4000), "video/mp4") == 2
    assert sleeps == [2.0]


@responses.activate
def test_last_chunk_206_is_incomplete(tmp_path):
    responses.put(UPLOAD_URL, status=206)
    with pytest.raises(UploadError, match="more bytes"):
        client()[0].upload(UPLOAD_URL, _video(tmp_path, 4000), plan_chunks(4000), "video/mp4")


# ---------- status ----------

@responses.activate
def test_status_polling_until_complete():
    for st in ("PROCESSING_UPLOAD", "PROCESSING_UPLOAD"):
        responses.post(API + "/v2/post/publish/status/fetch/", json=ok({"status": st, "uploaded_bytes": 10000}))
    responses.post(API + "/v2/post/publish/status/fetch/",
                   json=ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000000000000001]}))
    c, sleeps = client()
    seen = []
    final = c.wait_for_status("v_pub_file~v2-1.123456789", interval=5, on_update=lambda s: seen.append(s["status"]))
    assert final["publicaly_available_post_id"] == [7300000000000000001]
    assert seen == ["PROCESSING_UPLOAD", "PROCESSING_UPLOAD", "PUBLISH_COMPLETE"] and sleeps == [5, 5]
    assert json.loads(responses.calls[0].request.body) == {"publish_id": "v_pub_file~v2-1.123456789"}


@responses.activate
def test_status_failed_and_timeout():
    responses.post(API + "/v2/post/publish/status/fetch/",
                   json=ok({"status": "FAILED", "fail_reason": "picture_size_check_failed",
                            "publicaly_available_post_id": [], "uploaded_bytes": 10000}))
    assert client()[0].wait_for_status("p")["fail_reason"] == "picture_size_check_failed"
    responses.post(API + "/v2/post/publish/status/fetch/", json=ok({"status": "PROCESSING_UPLOAD"}))
    ticks = iter([0, 0, 100, 1000])
    final = client()[0].wait_for_status("p", timeout=50, clock=lambda: next(ticks))
    assert final["status"] == "PROCESSING_UPLOAD"


@responses.activate
def test_network_error_propagates():
    responses.post(API + "/v2/post/publish/creator_info/query/", body=requests.ConnectionError("down"))
    with pytest.raises(requests.ConnectionError):
        client()[0].creator_info()

"""TikTok's official APIs only: Login Kit OAuth v2 and the Content Posting API.

Built against developers.tiktok.com: Login Kit for Web, Manage User Access Tokens, Content
Posting API (Get Started, Query Creator Info, Direct Post, Upload to inbox, Media Transfer
Guide, Get Post Status, Content Sharing Guidelines).
"""
from __future__ import annotations

import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import requests

from .video import ChunkPlan

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_BASE = "https://open.tiktokapis.com"
TOKEN_PATH = "/v2/oauth/token/"
REVOKE_PATH = "/v2/oauth/revoke/"
CREATOR_INFO_PATH = "/v2/post/publish/creator_info/query/"
DIRECT_INIT_PATH = "/v2/post/publish/video/init/"
INBOX_INIT_PATH = "/v2/post/publish/inbox/video/init/"
STATUS_PATH = "/v2/post/publish/status/fetch/"
CANCEL_PATH = "/v2/post/publish/cancel/"
SCOPES = ("user.info.basic", "video.publish", "video.upload")
PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
FINAL_STATUSES = {"PUBLISH_COMPLETE", "FAILED", "SEND_TO_USER_INBOX"}
JSON_CT = "application/json; charset=UTF-8"


class TikTokError(RuntimeError):
    exit_code = 1

    def __init__(self, message: str, code: str = "", log_id: str = ""):
        super().__init__(message + (f" (log_id {log_id})" if log_id else ""))
        self.code = code
        self.log_id = log_id


class AuthError(TikTokError):
    """access_token_invalid or a refused token request. Fix: run `auth` again."""
    exit_code = 3


class ScopeError(TikTokError):
    """scope_not_authorized: the app or the user did not grant video.publish / video.upload."""
    exit_code = 3


class RateLimited(TikTokError):
    exit_code = 4


class PostingBlocked(TikTokError):
    """The creator cannot post right now (spam risk, banned, daily cap, app user cap)."""
    exit_code = 6


class UnauditedApp(TikTokError):
    """unaudited_client_can_only_post_to_private_accounts."""
    exit_code = 7


class PrivacyMismatch(TikTokError):
    exit_code = 7


class UploadError(TikTokError):
    exit_code = 8


class BadRequest(TikTokError):
    exit_code = 2


CODE_MAP = {
    "access_token_invalid": AuthError,
    "scope_not_authorized": ScopeError,
    "rate_limit_exceeded": RateLimited,
    "spam_risk_too_many_posts": PostingBlocked,
    "spam_risk_user_banned_from_posting": PostingBlocked,
    "spam_risk_too_many_pending_share": PostingBlocked,
    "reached_active_user_cap": PostingBlocked,
    "unaudited_client_can_only_post_to_private_accounts": UnauditedApp,
    "privacy_level_option_mismatch": PrivacyMismatch,
    "url_ownership_unverified": BadRequest,
    "invalid_param": BadRequest,
    "invalid_publish_id": BadRequest,
    "token_not_authorized_for_specified_publish_id": BadRequest,
    "publish_not_cancellable": BadRequest,
}
PLAIN = {
    "unaudited_client_can_only_post_to_private_accounts":
        "TikTok has not audited this app yet, so it can only post as SELF_ONLY to an account set to private",
    "spam_risk_too_many_posts": "this account has hit TikTok's daily posting cap, try again tomorrow",
    "spam_risk_user_banned_from_posting": "TikTok has blocked this account from posting",
    "reached_active_user_cap": "the app has reached its daily active-creator cap",
    "privacy_level_option_mismatch": "the chosen privacy level is not one this creator can use",
    "scope_not_authorized":
        "the token is missing video.publish or video.upload; check the app's scopes and sign in again",
    "access_token_invalid": "the access token is invalid or expired",
}


# ---------- Login Kit OAuth v2 ----------

def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorization_url(client_key: str, redirect_uri: str, state: str, scopes=SCOPES) -> str:
    query = urllib.parse.urlencode({
        "client_key": client_key,
        "response_type": "code",
        "scope": ",".join(scopes),       # TikTok wants commas, not spaces
        "redirect_uri": redirect_uri,
        "state": state,
    })
    return f"{AUTH_URL}?{query}"


def parse_redirect(redirected_url: str, expected_state: str) -> str:
    """TikTok only redirects to https URLs, so the owner pastes the address they land on.
    Returns the (URL-decoded) code after checking state."""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(redirected_url.strip()).query))
    if q.get("state") != expected_state:
        raise AuthError("state did not match, possible forged request; nothing was saved")
    if q.get("error"):
        raise AuthError(f"TikTok returned {q['error']}: {q.get('error_description', '')}".strip())
    if not q.get("code"):
        raise AuthError("that address has no code in it; paste the full address from the browser bar")
    granted = set(filter(None, q.get("scopes", "").split(",")))
    if granted and "video.publish" not in granted:
        raise ScopeError("video.publish was not granted on the consent screen; sign in again and allow it")
    return q["code"]


def _token_call(session: requests.Session, data: dict, base: str = API_BASE) -> dict:
    resp = session.post(base + TOKEN_PATH, data=data, timeout=30,
                        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or body.get("error") or not body.get("access_token"):
        err = body.get("error", f"http_{resp.status_code}")
        raise AuthError(f"TikTok token request failed: {err} {body.get('error_description', '')}".strip(),
                        code=str(err), log_id=body.get("log_id", ""))
    return body


def exchange_code(code: str, client_key: str, client_secret: str, redirect_uri: str,
                  session: Optional[requests.Session] = None, base: str = API_BASE) -> dict:
    return _token_call(session or requests.Session(), {
        "client_key": client_key, "client_secret": client_secret, "code": code,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri}, base)


def refresh_token(refresh: str, client_key: str, client_secret: str,
                  session: Optional[requests.Session] = None, base: str = API_BASE) -> dict:
    """Returns a new access token AND a new refresh token; the old refresh token is now dead."""
    return _token_call(session or requests.Session(), {
        "client_key": client_key, "client_secret": client_secret,
        "grant_type": "refresh_token", "refresh_token": refresh}, base)


def revoke(token: str, client_key: str, client_secret: str,
           session: Optional[requests.Session] = None, base: str = API_BASE) -> None:
    s = session or requests.Session()
    resp = s.post(base + REVOKE_PATH, data={"client_key": client_key, "client_secret": client_secret,
                                            "token": token},
                  headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if resp.status_code != 200:
        raise AuthError(f"revoke failed with HTTP {resp.status_code}")


# ---------- Content Posting API ----------

@dataclass
class CreatorInfo:
    username: str
    nickname: str
    privacy_level_options: List[str]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int


class TikTokClient:
    def __init__(self, access_token: str, session: Optional[requests.Session] = None, base: str = API_BASE,
                 max_retries: int = 3, sleep: Optional[Callable[[float], None]] = None,
                 refresh: Optional[Callable[[], str]] = None):
        self._token = access_token
        self.s = session or requests.Session()
        self.base = base.rstrip("/")
        self.max_retries = max_retries
        self.sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._refresh = refresh      # called once on access_token_invalid, returns a new token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": JSON_CT}

    def _post(self, path: str, body: Optional[dict] = None) -> dict:
        refreshed = False
        attempt = 0
        while True:
            kw = {"json": body} if body is not None else {}
            resp = self.s.post(self.base + path, headers=self._headers(), timeout=30, **kw)
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            err = payload.get("error") or {}
            code = err.get("code", "")
            if resp.status_code == 200 and code == "ok":
                return payload.get("data") or {}
            retryable = resp.status_code == 429 or code == "rate_limit_exceeded" or resp.status_code >= 500
            if retryable and attempt < self.max_retries:
                self.sleep(min(60.0, 2.0 ** (attempt + 1) * (5 if resp.status_code == 429 else 1)))
                attempt += 1
                continue
            if code == "access_token_invalid" and self._refresh and not refreshed:
                self._token = self._refresh()
                refreshed = True
                continue
            if not code:
                code = f"http_{resp.status_code}"
            cls = CODE_MAP.get(code, TikTokError)
            msg = PLAIN.get(code) or err.get("message") or f"TikTok returned {code}"
            raise cls(f"{msg} [{code}, HTTP {resp.status_code}]", code=code, log_id=err.get("log_id", ""))

    def creator_info(self) -> CreatorInfo:
        """Must be called fresh before each post (Content Sharing Guidelines). Spam and cap
        errors come back with HTTP 200 here, and are raised as PostingBlocked."""
        d = self._post(CREATOR_INFO_PATH)
        return CreatorInfo(
            username=d.get("creator_username", ""), nickname=d.get("creator_nickname", ""),
            privacy_level_options=list(d.get("privacy_level_options", [])),
            comment_disabled=bool(d.get("comment_disabled")), duet_disabled=bool(d.get("duet_disabled")),
            stitch_disabled=bool(d.get("stitch_disabled")),
            max_video_post_duration_sec=int(d.get("max_video_post_duration_sec", 0)))

    @staticmethod
    def file_source(plan: ChunkPlan) -> dict:
        return {"source": "FILE_UPLOAD", "video_size": plan.video_size, "chunk_size": plan.chunk_size,
                "total_chunk_count": plan.total_chunk_count}

    def init_direct_post(self, post_info: dict, source_info: dict) -> dict:
        if post_info.get("privacy_level") not in PRIVACY_LEVELS:
            raise BadRequest("post_info.privacy_level must be chosen explicitly; there is no default")
        return self._post(DIRECT_INIT_PATH, {"post_info": post_info, "source_info": source_info})

    def init_inbox(self, source_info: dict) -> dict:
        return self._post(INBOX_INIT_PATH, {"source_info": source_info})

    def upload(self, upload_url: str, path: str | Path, plan: ChunkPlan, content_type: str) -> int:
        """PUT each chunk in order to the full upload_url. Chunks before the last answer 206,
        the last answers 201. 5xx retries the same chunk. Returns the number of PUTs made."""
        puts = 0
        ranges = plan.ranges()
        with open(path, "rb") as fh:
            for i, (start, end) in enumerate(ranges):
                fh.seek(start)
                chunk = fh.read(end - start + 1)
                headers = {"Content-Type": content_type, "Content-Length": str(len(chunk)),
                           "Content-Range": f"bytes {start}-{end}/{plan.video_size}"}
                for attempt in range(self.max_retries + 1):
                    resp = self.s.put(upload_url, data=chunk, headers=headers, timeout=300)
                    puts += 1
                    if resp.status_code >= 500 and attempt < self.max_retries:
                        self.sleep(2.0 ** (attempt + 1))
                        continue
                    break
                last = i == len(ranges) - 1
                if resp.status_code == (201 if last else 206):
                    continue
                if last and resp.status_code == 206:
                    raise UploadError("TikTok still expects more bytes after the last chunk", code="incomplete")
                messages = {400: "TikTok rejected the chunk headers or size", 403: "the upload_url has expired "
                            "(it lasts one hour); start the post again", 404: "TikTok has no upload task for "
                            "this URL", 416: "Content-Range did not match TikTok's upload progress"}
                raise UploadError(messages.get(resp.status_code, f"chunk {i + 1} failed with HTTP "
                                               f"{resp.status_code}"), code=f"http_{resp.status_code}")
        return puts

    def fetch_status(self, publish_id: str) -> dict:
        return self._post(STATUS_PATH, {"publish_id": publish_id})

    def wait_for_status(self, publish_id: str, timeout: float = 600, interval: float = 5,
                        on_update: Optional[Callable[[dict], None]] = None,
                        clock: Callable[[], float] = time.monotonic) -> dict:
        """Poll status/fetch (30 requests a minute allowed) until PUBLISH_COMPLETE, FAILED or
        SEND_TO_USER_INBOX. Moderation is usually under a minute but can take hours, so a
        timeout returns the last status rather than calling it a failure."""
        deadline = clock() + timeout
        last: dict = {}
        while True:
            last = self.fetch_status(publish_id)
            if on_update:
                on_update(last)
            if last.get("status") in FINAL_STATUSES or clock() >= deadline:
                return last
            self.sleep(max(2.0, interval))

    def cancel(self, publish_id: str) -> None:
        self._post(CANCEL_PATH, {"publish_id": publish_id})

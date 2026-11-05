import os
import stat
import subprocess

import pytest

from tt_poster.tokens import (FileTokenStore, KeychainTokenStore, TokenStoreError, make_store, save_token_response,
                              seconds_left)
from tt_poster.video import (MB, VideoError, VideoInfo, check_limits, content_type, plan_chunks, probe)


def test_docs_worked_example():
    # Media Transfer Guide: 50,000,123 bytes with 10,000,000-byte chunks is 5 chunks,
    # the last one 40,000,000-50,000,122.
    plan = plan_chunks(50_000_123, 10_000_000)
    assert plan.total_chunk_count == 5
    assert plan.ranges()[0] == (0, 9_999_999)
    assert plan.ranges()[-1] == (40_000_000, 50_000_122)


def test_small_files_are_one_chunk():
    assert plan_chunks(4000).total_chunk_count == 1 and plan_chunks(4000).chunk_size == 4000
    p = plan_chunks(8 * MB)        # between 5 MB and the 10 MB default chunk
    assert (p.total_chunk_count, p.chunk_size) == (1, 8 * MB)


def test_remainder_goes_on_the_last_chunk():
    p = plan_chunks(25 * MB, 10 * MB)
    assert p.total_chunk_count == 2
    a, b = p.ranges()[-1]
    assert b - a + 1 == 15 * MB


def test_chunk_limits():
    for bad in (dict(video_size=0), dict(video_size=5 * 1024 * MB), dict(video_size=50 * MB, chunk_size=4 * MB),
                dict(video_size=50 * MB, chunk_size=65 * MB)):
        with pytest.raises(VideoError):
            plan_chunks(**bad)
    # 4 GB in 5 MB chunks is 819 chunks, so TikTok's 1000-chunk cap can't be hit within the size limit
    assert plan_chunks(4 * 1024 * MB, 5 * MB).total_chunk_count == 819


def test_content_types():
    assert content_type("a.MP4") == "video/mp4"
    assert content_type("a.mov") == "video/quicktime"
    assert content_type("a.webm") == "video/webm"
    with pytest.raises(VideoError):
        content_type("a.avi")


def test_probe_and_limits():
    out = '{"streams":[{"codec_name":"h264","width":1080,"height":1920,"r_frame_rate":"30000/1001"}],' \
          '"format":{"duration":"14.5"}}'
    runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")  # noqa: E731
    info = probe("x.mp4", runner=runner)
    assert info == VideoInfo(14.5, 1080, 1920, 29.97, "h264")
    assert check_limits(info, 300) == []
    bad = VideoInfo(400, 200, 5000, 15, "mpeg4")
    assert len(check_limits(bad, 300)) == 5
    with pytest.raises(VideoError):
        probe("x.mp4", runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bad"))


class FakeSecurity:
    def __init__(self):
        self.items, self.order = {}, []

    def __call__(self, cmd, input=None, capture_output=True, text=True):
        if cmd[1] == "find-generic-password":
            v = self.items.get(cmd[cmd.index("-s") + 1])
            return subprocess.CompletedProcess(cmd, 0 if v else 44, stdout=(v or "") + "\n", stderr="")
        parts = input.split('"')
        self.items[parts[3]] = parts[5]
        self.order.append(parts[3])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


TOKEN_RESPONSE = {"access_token": "act.example12345Example12345Example", "expires_in": 86400,
                  "open_id": "asdf-12345c-1a2s3d-ac98-asdf123as12as34", "refresh_expires_in": 31536000,
                  "refresh_token": "rft.example12345Example12345Example", "scope": "user.info.basic,video.list",
                  "token_type": "Bearer"}


def test_keychain_saves_refresh_token_first():
    sec = FakeSecurity()
    store = KeychainTokenStore(runner=sec)
    save_token_response(store, TOKEN_RESPONSE, now=1000)
    assert sec.order[0] == "tiktok-refresh-token"
    assert store.get("expires_at") == str(1000 + 86400)
    assert store.get("open_id") == TOKEN_RESPONSE["open_id"]
    with pytest.raises(TokenStoreError):
        store.save(access_token='x" ; rm')
    with pytest.raises(TokenStoreError):
        store.save(client_secret="x")
    with pytest.raises(TokenStoreError, match="tiktok-client-key"):
        store.get("client_key")


def test_file_store(tmp_path, monkeypatch):
    path = tmp_path / "t.json"
    store = FileTokenStore(path, environ={"TIKTOK_CLIENT_KEY": "ck"})
    save_token_response(store, TOKEN_RESPONSE)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert store.get("client_key") == "ck" and 86000 < seconds_left(store) <= 86400
    assert seconds_left(FileTokenStore(tmp_path / "none.json", environ={})) < 0
    with pytest.raises(TokenStoreError):
        store.get("client_secret")
    monkeypatch.setenv("TIKTOK_TOKEN_FILE", str(path))
    assert isinstance(make_store("file"), FileTokenStore) and isinstance(make_store("keychain"), KeychainTokenStore)
    with pytest.raises(TokenStoreError):
        make_store("nope")

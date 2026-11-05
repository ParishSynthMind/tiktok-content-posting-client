"""Rehearsal against a LOCAL fake of TikTok's Content Posting API (not TikTok itself).

Starts a small HTTP server that answers creator_info, video/init, the chunk PUTs and
status/fetch with the shapes from developers.tiktok.com, checks every Content-Range against what
it has received so far (416 if wrong), rebuilds the file, and compares its SHA-256 with the
original. Then runs the real publisher against it. Usage:
    python scripts/rehearsal_mock_server.py QUEUE_DIR
"""
import hashlib
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tt_poster.brief import load_brief  # noqa: E402
from tt_poster.publisher import PostedLog, publish_approved  # noqa: E402
from tt_poster.queue import ApprovalQueue  # noqa: E402
from tt_poster.tiktok import TikTokClient  # noqa: E402

LOG = "20261105LOCALREHEARSAL0000000000"
state = {"received": bytearray(), "total": None, "polls": 0, "puts": []}


def envelope(data):
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": LOG}}


class FakeTikTok(BaseHTTPRequestHandler):
    def _json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != "Bearer act.rehearsal":
            return self._json(401, {"error": {"code": "access_token_invalid", "message": "", "log_id": LOG}})
        if self.path == "/v2/post/publish/creator_info/query/":
            return self._json(200, envelope({
                "creator_avatar_url": "https://example.invalid/a.png", "creator_username": "rehearsal_account",
                "creator_nickname": "Rehearsal", "privacy_level_options": ["SELF_ONLY"],
                "comment_disabled": False, "duet_disabled": False, "stitch_disabled": False,
                "max_video_post_duration_sec": 180}))
        if self.path == "/v2/post/publish/video/init/":
            if body["post_info"]["privacy_level"] != "SELF_ONLY":
                return self._json(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts",
                                                  "message": "", "log_id": LOG}})
            state["total"] = body["source_info"]["video_size"]
            port = self.server.server_address[1]
            return self._json(200, envelope({"publish_id": "v_pub_file~v2-1.rehearsal",
                                             "upload_url": f"http://127.0.0.1:{port}/video/?upload_id=1&upload_token=t"}))
        if self.path == "/v2/post/publish/status/fetch/":
            state["polls"] += 1
            done = state["polls"] >= 2 and len(state["received"]) == state["total"]
            return self._json(200, envelope({"status": "PUBLISH_COMPLETE" if done else "PROCESSING_UPLOAD",
                                             "publicaly_available_post_id": [],
                                             "uploaded_bytes": len(state["received"])}))
        return self._json(404, {"error": {"code": "not_found", "message": self.path, "log_id": LOG}})

    def do_PUT(self):  # noqa: N802
        m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", self.headers.get("Content-Range", ""))
        chunk = self.rfile.read(int(self.headers["Content-Length"]))
        if not m or int(m.group(1)) != len(state["received"]) or int(m.group(2)) - int(m.group(1)) + 1 != len(chunk):
            self.send_response(416)
            self.end_headers()
            return
        state["received"] += chunk
        state["puts"].append((self.headers["Content-Range"], self.headers["Content-Type"], len(chunk)))
        self.send_response(201 if len(state["received"]) == int(m.group(3)) else 206)
        self.send_header("Content-Range", f"bytes 0-{len(state['received']) - 1}/{m.group(3)}")
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    queue_dir = Path(sys.argv[1])
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTikTok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Local fake TikTok API on {base} (rehearsal, not TikTok)")
    client = TikTokClient("act.rehearsal", base=base, sleep=lambda s: None)
    queue = ApprovalQueue(queue_dir / "queue.json")
    item = queue.approved()[0]
    run = publish_approved(queue, PostedLog(queue_dir / "posted-log.jsonl"), load_brief, client, dry_run=False)
    for cr, ct, n in state["puts"]:
        print(f"server got PUT {cr} ({ct}, {n} bytes)")
    original = hashlib.sha256(Path(item["video"]).read_bytes()).hexdigest()
    rebuilt = hashlib.sha256(bytes(state["received"])).hexdigest()
    print(f"original sha256 {original[:16]}..., rebuilt on server {rebuilt[:16]}..., match: {original == rebuilt}")
    server.shutdown()
    return 0 if original == rebuilt and run.exit_code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

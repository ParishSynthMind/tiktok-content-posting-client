"""The video side: content type, a chunk plan that follows TikTok's media transfer rules, and an
optional ffprobe check against TikTok's format limits.

Chunk rules (Media Transfer Guide): each chunk 5 MB to 64 MB; the last chunk takes the leftover
bytes and may be up to 128 MB; files under 5 MB go as one chunk; 1 to 1000 chunks, in order;
total_chunk_count = video_size // chunk_size.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
MAX_LAST_CHUNK = 128 * MB
MAX_CHUNKS = 1000
MAX_VIDEO_BYTES = 4 * 1024 * MB
DEFAULT_CHUNK = 10 * MB
CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


class VideoError(ValueError):
    pass


@dataclass
class ChunkPlan:
    video_size: int
    chunk_size: int
    total_chunk_count: int

    def ranges(self) -> List[Tuple[int, int]]:
        """(first_byte, last_byte) for every chunk, inclusive, the last one taking the remainder."""
        out = []
        for i in range(self.total_chunk_count):
            start = i * self.chunk_size
            end = self.video_size - 1 if i == self.total_chunk_count - 1 else start + self.chunk_size - 1
            out.append((start, end))
        return out


def plan_chunks(video_size: int, chunk_size: int = DEFAULT_CHUNK) -> ChunkPlan:
    if video_size <= 0:
        raise VideoError("the video file is empty")
    if video_size > MAX_VIDEO_BYTES:
        raise VideoError("TikTok accepts videos up to 4 GB")
    if not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
        raise VideoError("chunk size must be between 5 MB and 64 MB")
    if video_size < MIN_CHUNK or video_size <= chunk_size:
        if video_size > MAX_CHUNK:
            raise VideoError("a single chunk can be at most 64 MB")
        return ChunkPlan(video_size, video_size, 1)
    count = video_size // chunk_size
    plan = ChunkPlan(video_size, chunk_size, count)
    last_start, last_end = plan.ranges()[-1]
    if count > MAX_CHUNKS:
        raise VideoError("more than 1000 chunks; use a bigger chunk size")
    if last_end - last_start + 1 > MAX_LAST_CHUNK:
        raise VideoError("the last chunk would be over 128 MB")
    return plan


def content_type(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext not in CONTENT_TYPES:
        raise VideoError("TikTok takes MP4, MOV or WebM files")
    return CONTENT_TYPES[ext]


def file_digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(MB), b""):
            h.update(block)
    return h.hexdigest()[:16]


@dataclass
class VideoInfo:
    duration_sec: float
    width: int
    height: int
    fps: float
    codec: str


def probe(path: str | Path, runner=subprocess.run) -> Optional[VideoInfo]:
    """Duration, size, frame rate and codec via ffprobe, or None if ffprobe is not installed."""
    if runner is subprocess.run and not shutil.which("ffprobe"):
        return None
    proc = runner(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "stream=width,height,r_frame_rate,codec_name:format=duration", "-of", "json", str(path)],
                  capture_output=True, text=True)
    if proc.returncode != 0:
        raise VideoError("ffprobe could not read the video")
    data = json.loads(proc.stdout)
    stream = (data.get("streams") or [{}])[0]
    num, _, den = str(stream.get("r_frame_rate", "0/1")).partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return VideoInfo(float(data.get("format", {}).get("duration", 0)), int(stream.get("width", 0)),
                     int(stream.get("height", 0)), round(fps, 2), str(stream.get("codec_name", "")))


def check_limits(info: VideoInfo, max_duration_sec: Optional[int] = None) -> List[str]:
    """TikTok's documented limits: 23 to 60 fps, 360 to 4096 px each side, and the creator's
    max_video_post_duration_sec from creator_info."""
    problems = []
    if not 23 <= info.fps <= 60:
        problems.append(f"frame rate {info.fps} is outside 23 to 60 fps")
    for side, value in (("width", info.width), ("height", info.height)):
        if not 360 <= value <= 4096:
            problems.append(f"{side} {value}px is outside 360 to 4096 px")
    if info.codec not in ("h264", "hevc", "vp8", "vp9"):
        problems.append(f"codec {info.codec!r} is not H.264, H.265, VP8 or VP9")
    if max_duration_sec and info.duration_sec > max_duration_sec:
        problems.append(f"video is {info.duration_sec:.0f}s, this creator can post up to {max_duration_sec}s")
    return problems

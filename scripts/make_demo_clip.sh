#!/bin/sh
# Makes the 12-second vertical demo clip used by briefs/standing-desk.md (1080x1920, 30 fps, H.264).
# High bitrate on purpose, so the file is big enough to be uploaded in more than one chunk.
set -e
cd "$(dirname "$0")/.."
ffmpeg -y -loglevel error -f lavfi -i "testsrc2=size=1080x1920:rate=30" -t 12 \
  -c:v libx264 -pix_fmt yuv420p -b:v 18M -maxrate 18M -bufsize 36M briefs/demo-clip.mp4
ls -l briefs/demo-clip.mp4

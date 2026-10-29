#!/usr/bin/env bash
set -euo pipefail
# Requires an installed media-manager and FFmpeg. Only synthetic media is created.
DEMO_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/media-manager-demo.XXXXXX")
printf 'Disposable synthetic library: %s\n' "$DEMO_ROOT"
cd "$DEMO_ROOT"
mkdir Movies
ffmpeg -nostdin -hide_banner -loglevel error -f lavfi \
  -i color=c=blue:s=320x240:r=24 -t 2 -c:v libx264 -pix_fmt yuv420p \
  'Movies/Synthetic Demo (2026).mp4'
media-manager init
media-manager scan
media-manager plan
# Leave the synthetic library available for inspection; no repair command runs.

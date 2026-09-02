#!/usr/bin/env bash
set -euo pipefail
PREFIX=${PREFIX:-"$HOME/.local"}
rm -f "$PREFIX/bin/media-manager"
rm -rf "$PREFIX/lib/stateful-media-manager"
echo "Removed media-manager program files. Per-library .media-manager state was left untouched."

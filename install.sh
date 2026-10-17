#!/usr/bin/env bash
set -euo pipefail

PREFIX=${PREFIX:-"$HOME/.local"}
LIBDIR="$PREFIX/lib/stateful-media-manager"
BINDIR="$PREFIX/bin"

python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"media-manager requires Python 3.11+; found {sys.version.split()[0]}")
try:
    import numpy
except ImportError:
    raise SystemExit(
        "media-manager requires NumPy for native-pixel crop analysis. "
        "Install with pip/pipx first (for example: python3 -m pip install --user 'numpy>=1.26')."
    )
PY

mkdir -p "$LIBDIR" "$BINDIR"
rm -rf "$LIBDIR/media_manager"
cp -a media_manager "$LIBDIR/media_manager"
cp -f README.md LICENSE pyproject.toml "$LIBDIR/"

cat > "$BINDIR/media-manager" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m media_manager "\$@"
EOF
chmod 755 "$BINDIR/media-manager"

echo "Installed media-manager to: $BINDIR/media-manager"
echo "Library code: $LIBDIR/media_manager"
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    echo "WARNING: ffmpeg/ffprobe are required but not both available in PATH." >&2
fi
if ! command -v ffsubsync >/dev/null 2>&1 && ! command -v ffs >/dev/null 2>&1; then
    echo "WARNING: ffsubsync/ffs is not in PATH; subtitle synchronization will be unavailable." >&2
fi
case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) echo "NOTE: add $BINDIR to PATH if 'media-manager' is not found." ;;
esac

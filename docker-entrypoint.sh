#!/bin/sh
set -eu

# App state is independent of every media mount. Never chown media folders.
if [ "$(id -u)" = "0" ]; then
    app_uid=${MEDIA_MANAGER_UID:-1000}
    app_gid=${MEDIA_MANAGER_GID:-1000}
    case "$app_uid:$app_gid" in
        *[!0-9:]*|:*|*:) echo "Invalid MEDIA_MANAGER_UID/GID" >&2; exit 1 ;;
    esac
    mkdir -p /state /state/models "$HOME"
    chown "$app_uid:$app_gid" /state /state/models "$HOME"
    exec setpriv --reuid="$app_uid" --regid="$app_gid" --clear-groups "$@"
fi
exec "$@"

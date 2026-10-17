#!/bin/sh
set -eu

run_as_library_owner=""
if [ "$(id -u)" = "0" ] && [ -d /library ]; then
    library_uid=$(stat -c '%u' /library)
    library_gid=$(stat -c '%g' /library)
    if [ "$library_uid" != "0" ]; then
        mkdir -p "$HOME"
        chown "$library_uid:$library_gid" "$HOME"
        run_as_library_owner="setpriv --reuid=$library_uid --regid=$library_gid --clear-groups"
    fi
fi

if [ "$#" -ge 2 ] && [ "$1" = "media-manager" ] && [ "$2" = "web" ] \
    && [ ! -f /library/.media-manager/config.toml ]; then
    if [ -n "$run_as_library_owner" ]; then
        # shellcheck disable=SC2086
        $run_as_library_owner sh -c 'cd /library && exec media-manager init'
    else
        cd /library
        media-manager init
    fi
fi

if [ -n "$run_as_library_owner" ]; then
    # shellcheck disable=SC2086
    exec $run_as_library_owner "$@"
fi
exec "$@"

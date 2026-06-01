#!/bin/sh
# Entrypoint that fixes the #1 self-host footgun: a Docker container
# that runs as root writes root-owned files into bind-mounted host
# volumes, and the host user (or a non-root service user) then can't
# update them — silently breaking skill extraction, prefs saves, mail
# attachments, etc.
#
# Standard PUID/PGID pattern: pick the UID/GID we should drop to,
# chown the writable bind-mounts so existing root-owned content gets
# repaired on every start (idempotent), then exec the real command
# as that user via gosu.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Reuse an existing matching group/user if the host's UID/GID already
# corresponds to one in /etc/passwd (e.g. when the image is rebuilt
# and "radian" already exists at the same id). Otherwise create.
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" radian
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -s /bin/sh -d /app radian
fi

# Repair ownership on every writable path the app touches at runtime.
#
# Bind-mounted dirs (/app/data, /app/logs) are the obvious ones, but
# the app ALSO writes inside the image's own source tree at runtime:
#   - services/cache/{search,content}/*  (search cache LRU)
#   - services/search_analytics.json
#   - services/search_engine_error.log
#   - services/tts cache, etc.
# These dirs were created as root during `docker build`, so dropping
# to PUID:PGID would otherwise crash on the first import that tries
# to mkdir them. Chown the whole /app tree — fast (<1s on this size)
# and idempotent via the `-not -uid` filter so we only touch files
# that need fixing.
for dir in /app /app/data /app/logs; do
    if [ -d "$dir" ]; then
        # `find ... -not -uid` keeps this O(touched-files), not
        # O(everything), so terabyte-sized maildirs don't slow startup.
        find "$dir" -not -uid "$PUID" -print0 2>/dev/null \
            | xargs -0 -r chown "$PUID:$PGID" 2>/dev/null || true
    fi
done

# Drop root and run the actual app. `gosu` is preferred over `su` /
# `sudo` because it cleans up the process tree (no extra shell layer)
# so signals (SIGTERM from `docker stop`) reach uvicorn directly.
exec gosu "$PUID:$PGID" "$@"

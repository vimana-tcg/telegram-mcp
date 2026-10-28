#!/bin/bash
# Re-posts the product stories (24 h each). Run on the server: repost.sh [product key ...]
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd)
IMAGE=${IMAGE:-telegram-mcp:eddf5c2}
MEDIA=${MEDIA:-/opt/telegram-mcp/tg-media/white-gensets/reels/price}
STATE=${STATE_DIR:-/var/lib/story-passport}
R=/dev/shm/story-repost-$$.env; trap "shred -u $R 2>/dev/null||rm -f $R" EXIT
(umask 077; PW="$(cat /opt/telegram-mcp/.radar.key)" openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in /opt/telegram-mcp/.env.enc -pass env:PW > $R)
docker run --rm --env-file $R --memory 512m -v "$D":/app:ro -v "$MEDIA":/media:ro -v "$STATE":/state \
  --entrypoint python3 "$IMAGE" -u /app/repost.py "$@"

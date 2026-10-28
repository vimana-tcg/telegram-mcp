#!/bin/bash
# Started by systemd (story-passport.service). Decrypts the Telegram session into RAM,
# hands it to the container, then wipes the file once docker has read it.
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd)
IMAGE=${IMAGE:-telegram-mcp:eddf5c2}
DATA=${DATA:-/opt/telegram-mcp/tg-media/passports}
STATE=${STATE_DIR:-/var/lib/story-passport}
mkdir -p "$STATE"
R=/dev/shm/story-passport-$$.env; trap "shred -u $R 2>/dev/null||rm -f $R" EXIT
(umask 077; PW="$(cat /opt/telegram-mcp/.radar.key)" openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in /opt/telegram-mcp/.env.enc -pass env:PW > $R)
NAME=story-passport; SM=
if [ "${SP_DRY:-}" = 1 ]; then
  echo "SP_DRY=1" >> $R; NAME=story-passport-dry; SM=:ro   # dry run never touches the live container or state
else
  docker rm -f $NAME >/dev/null 2>&1 || true
fi
trap "docker stop $NAME >/dev/null 2>&1; shred -u $R 2>/dev/null||rm -f $R" TERM
docker run --rm --name $NAME --env-file $R --memory 256m \
  -v "$D":/app:ro -v "$DATA":/passports:ro -v "$STATE":/state$SM \
  --entrypoint python3 "$IMAGE" -u /app/responder.py &
sleep 10; shred -u $R 2>/dev/null || rm -f $R
wait

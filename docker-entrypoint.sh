#!/bin/sh
set -eu

mkdir -p /app/data
chown -R bridge:bridge /app/data
chmod 0775 /app/data

if [ "$#" -eq 0 ]; then
  set -- uvicorn app.main:app --host 0.0.0.0 --port 8095
fi

if [ "${P2P_BACKEND:-}" = "vendor" ]; then
  exec "$@"
fi

exec gosu bridge "$@"

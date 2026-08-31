#!/bin/sh
set -eu

mkdir -p /app/data
chown bridge:bridge /app/data

if [ "$#" -eq 0 ]; then
  set -- uvicorn app.main:app --host 0.0.0.0 --port 8095
fi

exec gosu bridge "$@"

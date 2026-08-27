#!/bin/sh
set -eu

mkdir -p /app/data
chown bridge:bridge /app/data

exec gosu bridge "$@"


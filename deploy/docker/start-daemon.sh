#!/usr/bin/env bash
set -euo pipefail

if docker info >/dev/null 2>&1; then
  exit 0
fi

mkdir -p /root/autodl-tmp/docker-data-vfs
nohup dockerd --config-file=/etc/docker/daemon.json \
  >/root/autodl-tmp/docker-daemon.log 2>&1 </dev/null &

for _ in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done

tail -n 50 /root/autodl-tmp/docker-daemon.log >&2
exit 1

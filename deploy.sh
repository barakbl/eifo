#!/usr/bin/env bash
# Ship this checkout to the server and restart it.
#
# Written after deploying by hand twice failed the same way: syncing only
# `packages/` leaves the server's uv.lock describing a different release, and
# `uv sync --locked` refuses to build against a lock that does not match. The
# fix is not to remember harder - it is for one list to say what a deployment
# consists of.
#
# COPYFILE_DISABLE stops macOS shipping an AppleDouble `._*` beside every file;
# without it a first sync sent 66,000 of them.
#
#   ./deploy.sh                 # code, then rebuild and restart
#   ./deploy.sh --rescore       # ...and recompute every score afterwards
set -euo pipefail

HOST="${EIFO_DEPLOY_HOST:-ubuntu@151.145.94.93}"
KEY="${EIFO_DEPLOY_KEY:-$HOME/.ssh/id_ed25519_oracle}"
REMOTE="${EIFO_DEPLOY_DIR:-~/eifo}"
SSH=(ssh -i "$KEY" -o ConnectTimeout=20 "$HOST")
COMPOSE="sudo docker compose -f docker-compose.yml -f docker-compose.proxy.yml"

# Everything the image is built from. The root files are here because leaving
# them out is the mistake this script exists to stop.
DIRS=(packages web)
FILES=(uv.lock pyproject.toml version.txt Dockerfile)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "Syncing to $HOST"
for d in "${DIRS[@]}"; do
  COPYFILE_DISABLE=1 rsync -az --delete \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='node_modules' \
    -e "ssh -i $KEY -o ConnectTimeout=20" "$d/" "$HOST:$REMOTE/$d/"
  echo "  $d/"
done
for f in "${FILES[@]}"; do
  COPYFILE_DISABLE=1 rsync -az -e "ssh -i $KEY -o ConnectTimeout=20" "$f" "$HOST:$REMOTE/$f"
  echo "  $f"
done

# Before anything that writes: the database is the one thing here that cannot
# be rebuilt from the repository.
say "Backing up the catalog"
"${SSH[@]}" "sudo cp $REMOTE/data/eifo.db $REMOTE/data/eifo.db.before-deploy && \
             sudo du -h $REMOTE/data/eifo.db.before-deploy | cut -f1 | sed 's/^/  /'"

say "Building"
"${SSH[@]}" "cd $REMOTE && $COMPOSE build api" | tail -3

say "Restarting"
"${SSH[@]}" "cd $REMOTE && $COMPOSE up -d" | tail -4

if [[ "${1:-}" == "--rescore" ]]; then
  say "Rescoring every title"
  # -u and a line per thousand: a rescore that says nothing for minutes is
  # indistinguishable from one that has hung, and there is no way to tell from
  # outside which it is.
  "${SSH[@]}" "sudo docker exec eifo-api-1 python -u -c '
from sqlalchemy import func, select
from eifo_core.db import create_engine_from_settings, make_session_factory
from eifo_core.enriching import recompute
from eifo_core.models import Title
from eifo_core.settings import get_settings
settings = get_settings()
factory = make_session_factory(create_engine_from_settings(settings))
changed = seen = 0
with factory() as session:
    total = session.scalar(select(func.count()).select_from(Title)) or 0
    print(f\"  {total:,} titles to look at\")
    for title in session.scalars(select(Title)):
        seen += 1
        if recompute(session, title, settings):
            changed += 1
        if seen % 1000 == 0:
            print(f\"  {seen:,} / {total:,}  ({seen * 100 // total}%)  {changed:,} changed\")
    session.commit()
print(f\"  rescored {changed:,} of {seen:,} titles\")
'"
fi

# A container that has just started is not one that is serving: the first check
# after `up -d` reported 502 on a deployment that was entirely fine, which is
# the kind of false alarm that teaches you to ignore the check.
say "Waiting for it to come up"
for _ in $(seq 1 30); do
  if "${SSH[@]}" "sudo docker inspect eifo-api-1 --format '{{.State.Health.Status}}'" 2>/dev/null \
       | grep -q healthy; then
    break
  fi
  sleep 3
done

say "Checking it answers"
"${SSH[@]}" "cd $REMOTE && sudo docker ps --format '  {{.Names}}|{{.Status}}'"
curl -sS -m 20 -o /dev/null -w "  HTTPS: %{http_code} in %{time_total}s\n" https://eifo.barakbloch.com/

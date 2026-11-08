#!/usr/bin/env bash
# Try the monitoring stack on this machine, against an Eifo API run from this
# checkout.
#
#   monitoring/dev.sh up         start it, and say how to start the API to match
#   monitoring/dev.sh history    import the local database's run history
#   monitoring/dev.sh down       stop it (down -v also deletes what it stored)
#
# Grafana is then at http://localhost:3480/grafana/, behind the same proxy
# rules the server gets, so it lets in whoever is signed in to the Eifo at
# http://localhost:3480/ as an administrator.
set -euo pipefail

cd "$(dirname "$0")"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.dev.yml)
ORIGIN="http://localhost:3480"

case "${1:-}" in
  up)
    docker network inspect eifo_default >/dev/null 2>&1 \
      || docker network create eifo_default >/dev/null
    mkdir -p secrets
    [[ -s secrets/eifo-metrics-token ]] || openssl rand -hex 32 | tr -d '\n' > secrets/eifo-metrics-token
    if [[ ! -s .env ]]; then
      printf 'EIFO_ORIGIN=%s\nGRAFANA_ADMIN_PASSWORD=%s\nGRAFANA_COOKIE_SECURE=false\n' \
        "$ORIGIN" "$(openssl rand -hex 24)" > .env
    fi
    "${COMPOSE[@]}" up -d
    cat <<TEXT

Start Eifo's API from the repository root with the same token:

  EIFO_METRICS_TOKEN=\$(cat monitoring/secrets/eifo-metrics-token) \\
  EIFO_PUBLIC_ORIGIN=$ORIGIN uv run eifo-api

Then sign in at $ORIGIN/ and open $ORIGIN/grafana/
TEXT
    ;;
  history)
    mkdir -p history
    (cd .. && uv run python -m eifo_api.metrics.history) > history/history.om
    "${COMPOSE[@]}" run --rm --no-deps -T \
      -v "$PWD/history/history.om:/history.om:ro" \
      --entrypoint promtool prometheus \
      tsdb create-blocks-from openmetrics --max-block-duration=720h /history.om /prometheus \
      | tail -1
    "${COMPOSE[@]}" restart prometheus >/dev/null
    rm -f history/history.om
    ;;
  down)
    "${COMPOSE[@]}" down "${@:2}"
    ;;
  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac

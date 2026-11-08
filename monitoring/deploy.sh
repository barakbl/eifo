#!/usr/bin/env bash
# Put the monitoring stack on the server Eifo runs on, beside it.
#
#   monitoring/deploy.sh --host ubuntu@203.0.113.7 --key ~/.ssh/id_ed25519
#   monitoring/deploy.sh ... --dry-run    # show what would change; change nothing
#   monitoring/deploy.sh ... --history    # import the run history again
#
# EIFO_DEPLOY_HOST and EIFO_DEPLOY_KEY work in place of --host and --key. Deploy
# Eifo first: this needs an API that serves /api/v1/metrics.
#
# Every step checks first and does nothing when there is nothing to do, so
# running it again is safe:
#
#   1. copies monitoring/ to ~/eifo-monitoring on the server
#   2. makes the secrets, once: a metrics token, written both to Eifo's .env
#      (EIFO_METRICS_TOKEN) and to Prometheus's secrets/, and a Grafana admin
#      password that nobody needs to know
#   3. starts Prometheus, Grafana, node-exporter and cAdvisor
#   4. puts Eifo's proxy on the monitoring network, through a compose file of
#      its own beside Eifo's (docker-compose.monitoring.yml), and restarts the
#      API if the token is new to it. Both are seconds of downtime, and only
#      the first time
#   5. adds the /grafana rules inside Eifo's site block in the proxy's
#      Caddyfile, between markers. Caddy checks the result before it is
#      loaded, and the old file goes back if Caddy refuses it
#   6. imports the run history Eifo has recorded, the first time only
#   7. checks that everything answers
#
# Never overwritten: the secrets, Eifo's .env beyond adding the one line, and
# what Prometheus and Grafana have stored.
#
# Whatever deploys Eifo must keep the proxy on the monitoring network: it has
# to pass docker-compose.monitoring.yml to `docker compose -f ...` whenever that
# file exists. Otherwise the next Eifo deploy puts Caddy back without it, and
# /grafana answers 502 until this runs again.
set -euo pipefail

cd "$(dirname "$0")"

HOST="${EIFO_DEPLOY_HOST:-}"
KEY="${EIFO_DEPLOY_KEY:-}"
EIFO_DIR="eifo"                 # Eifo's directory on the server, under home
REMOTE="eifo-monitoring"        # this stack's, under home
PROXY_SERVICE="proxy"           # the compose service that runs Caddy
DRY=0
HISTORY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="${2:?--host needs user@host}"; shift ;;
    --key) KEY="${2:?--key needs a path}"; shift ;;
    --eifo-dir) EIFO_DIR="${2:?}"; shift ;;
    --proxy-service) PROXY_SERVICE="${2:?}"; shift ;;
    --dry-run) DRY=1 ;;
    --history) HISTORY=1 ;;
    -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[[ -n "$HOST" ]] || die "Say which server: --host user@host, or set EIFO_DEPLOY_HOST."
SSH_OPTS=(-o ConnectTimeout=20)
[[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")
SSH=(ssh "${SSH_OPTS[@]}" "$HOST")
remote() { "${SSH[@]}" bash -s -- "$@"; }

# ---- What is there ------------------------------------------------------------

say "Looking at $HOST"
# One line per fact, read into variables below. Fails loudly on anything this
# cannot work without.
facts="$(remote "$EIFO_DIR" "$PROXY_SERVICE" <<'REMOTE'
set -euo pipefail
eifo_dir="$HOME/$1" proxy_service=$2
fail() { echo "FAIL $*"; exit 0; }

[ -f "$eifo_dir/.env" ] || fail "no $eifo_dir/.env - is Eifo deployed there?"
container() {
  sudo docker ps -q \
    --filter "label=com.docker.compose.project.working_dir=$eifo_dir" \
    --filter "label=com.docker.compose.service=$1" | head -1
}
api="$(container api)"
proxy="$(container "$proxy_service")"
[ -n "$api" ] || fail "Eifo's api container is not running"
[ -n "$proxy" ] || fail "no running '$proxy_service' service in $eifo_dir - see --proxy-service"
if sudo docker exec "$api" python -c "import eifo_api.metrics.history" 2>/dev/null; then
  echo "METRICS yes"
else
  echo "METRICS no"
fi

origin="$(grep -E '^EIFO_PUBLIC_ORIGIN=' "$eifo_dir/.env" | tail -1 | cut -d= -f2- | tr -d "\"' ")"
[ -n "$origin" ] || fail "EIFO_PUBLIC_ORIGIN is not set in $eifo_dir/.env"

caddyfile="$eifo_dir/Caddyfile"
mount="$(sudo docker inspect "$proxy" \
  --format '{{range .Mounts}}{{.Source}} {{.Destination}}{{"\n"}}{{end}}' \
  | awk -v src="$caddyfile" '$1 == src {print $2}')"
[ -n "$mount" ] || fail "the proxy does not mount $caddyfile"

echo "API $(sudo docker inspect "$api" --format '{{.Name}}' | tr -d /)"
echo "PROXY $(sudo docker inspect "$proxy" --format '{{.Name}}' | tr -d /)"
echo "ORIGIN $origin"
echo "NETWORK $(sudo docker inspect "$api" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')"
echo "FILES $(sudo docker inspect "$proxy" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
echo "CADDYFILE $caddyfile"
echo "MOUNT $mount"
echo "ATTACHED $(sudo docker inspect "$proxy" --format '{{if index .NetworkSettings.Networks "eifo-monitoring"}}yes{{else}}no{{end}}')"
grep -q '^EIFO_METRICS_TOKEN=.' "$eifo_dir/.env" && echo "TOKEN yes" || echo "TOKEN no"
[ -f "$HOME/eifo-monitoring/.history-imported" ] && echo "HISTORY yes" || echo "HISTORY no"
REMOTE
)"
fact() { awk -v key="$1" '$1 == key { $1 = ""; sub(/^ /, ""); print; exit }' <<<"$facts"; }
if grep -q '^FAIL' <<<"$facts"; then die "$(fact FAIL)"; fi

API="$(fact API)"
PROXY="$(fact PROXY)"
ORIGIN="$(fact ORIGIN)"
NETWORK="$(fact NETWORK)"
FILES="$(fact FILES)"
CADDYFILE="$(fact CADDYFILE)"
MOUNT="$(fact MOUNT)"
SITE="${ORIGIN#*://}"
SITE="${SITE%%/*}"
echo "  Eifo at $ORIGIN (container $API, network $NETWORK)"
echo "  proxy $PROXY, config $CADDYFILE"
echo "  on the monitoring network: $(fact ATTACHED); metrics token: $(fact TOKEN); history imported: $(fact HISTORY)"
if [[ "$(fact METRICS)" != yes ]]; then
  [[ $DRY == 1 ]] || die "Eifo on the server predates /api/v1/metrics - deploy Eifo first."
  echo "  Eifo on the server predates /api/v1/metrics: deploy Eifo before this, for real"
fi

# Removes the managed lines, then puts the current ones back right after the
# line that opens the site's block. Exit 3 if there is no such block.
MERGE_AWK='
function opens(line,    rest) {
  sub(/^[ \t]+/, "", line)
  sub(/^https?:\/\//, "", line)
  if (index(line, site) != 1) return 0
  rest = substr(line, length(site) + 1)
  return rest ~ /^(:[0-9]+)?([ \t]*,[^{]*)?[ \t]*\{[ \t]*$/
}
/^[ \t]*# >>> eifo-monitoring/ { skip = 1; next }
/^[ \t]*# <<< eifo-monitoring/ { skip = 0; next }
skip { next }
{ print }
!done && opens($0) {
  while ((getline line < region) > 0) print line
  close(region)
  done = 1
}
END { if (!done) exit 3 }
'

# ---- Dry run: report and stop ---------------------------------------------------

if [[ $DRY == 1 ]]; then
  say "Dry run - nothing is changed"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  "${SSH[@]}" "cat $CADDYFILE" > "$tmp/current"
  if ! awk -v site="$SITE" -v region="caddy/eifo-monitoring.caddy" "$MERGE_AWK" \
       "$tmp/current" > "$tmp/new"; then
    die "no site block for $SITE in $CADDYFILE"
  fi
  echo "  Caddyfile change:"
  diff -u "$tmp/current" "$tmp/new" | sed 's/^/    /' || true
  cmp -s "$tmp/current" "$tmp/new" && echo "    none"
  if command -v docker >/dev/null 2>&1; then
    # The server's own Caddy version. Syntax only: it cannot see the
    # server's network or certificates.
    image="$("${SSH[@]}" "sudo docker inspect $PROXY --format '{{.Config.Image}}'")"
    version="$("${SSH[@]}" "sudo docker exec $PROXY caddy version" | awk '{print $1}')"
    if docker run --rm -v "$tmp/new:/etc/caddyfile:ro" "caddy:${version#v}-alpine" \
         caddy validate --config /etc/caddyfile --adapter caddyfile >/dev/null 2>&1; then
      echo "  the new Caddyfile is valid for Caddy $version ($image on the server)"
    else
      die "the new Caddyfile does not validate with Caddy $version"
    fi
  fi
  [[ "$(fact ATTACHED)" == yes ]] || echo "  would restart the proxy onto the monitoring network"
  [[ "$(fact TOKEN)" == yes ]] || echo "  would add EIFO_METRICS_TOKEN to Eifo's .env and restart its API"
  [[ "$(fact HISTORY)" == yes && $HISTORY == 0 ]] || echo "  would import the run history"
  exit 0
fi

# ---- 1. Files -------------------------------------------------------------------

say "Copying monitoring/ to $HOST:$REMOTE"
# --delete never touches what is excluded, so the server's .env, secrets and
# any history file survive it.
COPYFILE_DISABLE=1 rsync -az --delete \
  --exclude='.env' --exclude='secrets/' --exclude='history/' --exclude='.history-imported' \
  --exclude='__pycache__' --exclude='dev.sh' --exclude='docker-compose.dev.yml' \
  --exclude='caddy/Caddyfile.dev' \
  -e "ssh ${SSH_OPTS[*]}" ./ "$HOST:$REMOTE/"

# ---- 2. Secrets ------------------------------------------------------------------

say "Secrets"
api_restart="$(remote "$EIFO_DIR" "$REMOTE" "$ORIGIN" "$NETWORK" <<'REMOTE'
set -euo pipefail
eifo_env="$HOME/$1/.env" here="$HOME/$2" origin=$3 network=$4
cd "$here"

restart=no
if ! grep -q '^EIFO_METRICS_TOKEN=.' "$eifo_env"; then
  cp "$eifo_env" "$eifo_env.before-monitoring"
  printf '\n# What Prometheus presents to read /api/v1/metrics (monitoring/deploy.sh).\nEIFO_METRICS_TOKEN=%s\n' \
    "$(openssl rand -hex 32)" >> "$eifo_env"
  echo "  added EIFO_METRICS_TOKEN to Eifo's .env (previous file kept as .env.before-monitoring)" >&2
  restart=yes
fi
token="$(grep '^EIFO_METRICS_TOKEN=' "$eifo_env" | tail -1 | cut -d= -f2-)"

mkdir -p secrets
held="$(sudo cat secrets/eifo-metrics-token 2>/dev/null || true)"
if [ "$held" != "$token" ]; then
  printf '%s' "$token" | sudo tee secrets/eifo-metrics-token >/dev/null
  echo "  wrote Prometheus's copy of the token" >&2
fi
# Prometheus runs as nobody (65534) in its container.
sudo chown 65534:65534 secrets/eifo-metrics-token
sudo chmod 0400 secrets/eifo-metrics-token

if [ ! -s .env ]; then
  umask 077
  printf 'EIFO_ORIGIN=%s\nEIFO_NETWORK=%s\nGRAFANA_ADMIN_PASSWORD=%s\n' \
    "$origin" "$network" "$(openssl rand -hex 24)" > .env
  echo "  wrote .env" >&2
else
  sed -i "s|^EIFO_ORIGIN=.*|EIFO_ORIGIN=$origin|; s|^EIFO_NETWORK=.*|EIFO_NETWORK=$network|" .env
fi
echo "$restart"
REMOTE
)"

# ---- 3. The stack ------------------------------------------------------------------

say "Starting Prometheus, Grafana and the exporters"
"${SSH[@]}" "cd $REMOTE && sudo docker compose pull -q && sudo docker compose up -d 2>&1 | tail -6"

# ---- 4. The proxy onto the monitoring network ----------------------------------------

say "Connecting the proxy"
remote "$EIFO_DIR" "$PROXY_SERVICE" "$FILES" "$api_restart" "$API" <<'REMOTE'
set -euo pipefail
eifo_dir="$HOME/$1" service=$2 files=$3 restart_api=$4 api=$5
overlay="$eifo_dir/docker-compose.monitoring.yml"
cd "$eifo_dir"

cat > "$overlay.new" <<YAML
# Written by monitoring/deploy.sh. Puts the proxy on the monitoring network
# too, which is the only way to reach Grafana. Pass this file to
# \`docker compose -f ...\` with the others whenever the proxy is started.
services:
  $service:
    networks:
      - default
      - monitoring

networks:
  monitoring:
    name: eifo-monitoring
    external: true
YAML
changed=no
if ! cmp -s "$overlay.new" "$overlay" 2>/dev/null; then
  mv "$overlay.new" "$overlay"
  changed=yes
else
  rm "$overlay.new"
fi

args=()
IFS=, read -ra listed <<<"$files"
for file in "${listed[@]}"; do
  [ "$file" = "$overlay" ] || args+=(-f "$file")
done
args+=(-f "$overlay")

attached="$(sudo docker ps -q --filter "label=com.docker.compose.project.working_dir=$eifo_dir" \
  --filter "label=com.docker.compose.service=$service" \
  | xargs -r sudo docker inspect --format '{{if index .NetworkSettings.Networks "eifo-monitoring"}}yes{{end}}')"
case ",$files," in *",$overlay,"*) listed_overlay=yes ;; *) listed_overlay=no ;; esac

if [ "$attached" != yes ] || [ "$listed_overlay" != yes ] || [ "$changed" = yes ]; then
  echo "  restarting the proxy onto the monitoring network"
  sudo docker compose "${args[@]}" up -d --no-deps "$service" 2>&1 | tail -2 | sed 's/^/  /'
else
  echo "  already on it"
fi

if [ "$restart_api" = yes ]; then
  echo "  restarting the API so it reads the metrics token"
  sudo docker compose "${args[@]}" up -d --no-deps api 2>&1 | tail -2 | sed 's/^/  /'
  for _ in $(seq 1 40); do
    status="$(sudo docker ps -q --filter "label=com.docker.compose.project.working_dir=$eifo_dir" \
      --filter label=com.docker.compose.service=api \
      | xargs -r sudo docker inspect --format '{{.State.Health.Status}}')"
    [ "$status" = healthy ] && break
    sleep 3
  done
  echo "  API is $status"
fi
REMOTE

# ---- 5. The proxy's rules ------------------------------------------------------------

say "Updating the proxy's Caddyfile"
"${SSH[@]}" "cat > $REMOTE/caddy-merge.awk" <<<"$MERGE_AWK"
remote "$CADDYFILE" "$MOUNT" "$SITE" "$REMOTE" "$EIFO_DIR" "$PROXY_SERVICE" <<'REMOTE'
set -euo pipefail
file=$1 config=$2 site=$3 here="$HOME/$4" eifo_dir="$HOME/$5" service=$6
container="$(sudo docker ps -q --filter "label=com.docker.compose.project.working_dir=$eifo_dir" \
  --filter "label=com.docker.compose.service=$service" | head -1)"
new="$(mktemp)"
trap 'rm -f "$new"' EXIT

if ! awk -v site="$site" -v region="$here/caddy/eifo-monitoring.caddy" \
     -f "$here/caddy-merge.awk" "$file" > "$new"; then
  echo "  no site block for $site in $file; nothing changed" >&2
  exit 1
fi
if cmp -s "$new" "$file"; then
  echo "  unchanged"
  exit 0
fi

cp "$file" "$file.before-monitoring"
# Written in place, not moved: the proxy bind-mounts this one file, and a
# rename would leave the container reading the old one.
cat "$new" > "$file"
restore() { cat "$file.before-monitoring" > "$file"; }

if ! out="$(sudo docker exec "$container" caddy validate --config "$config" --adapter caddyfile 2>&1)"; then
  restore
  echo "$out" | tail -5
  echo "  Caddy refused the new config; the old one is back and was never unloaded" >&2
  exit 1
fi
if ! sudo docker exec "$container" caddy reload --config "$config" --adapter caddyfile >/dev/null 2>&1; then
  restore
  sudo docker exec "$container" caddy reload --config "$config" --adapter caddyfile >/dev/null 2>&1 || true
  echo "  reload failed; the old config is back" >&2
  exit 1
fi
echo "  reloaded (previous file kept as $file.before-monitoring)"
REMOTE

# ---- 6. History --------------------------------------------------------------------

if [[ "$(fact HISTORY)" != yes || $HISTORY == 1 ]]; then
  say "Importing the run history"
  remote "$REMOTE" "$API" <<'REMOTE'
set -euo pipefail
cd "$HOME/$1"
api=$2
mkdir -p history
sudo docker exec "$api" python -m eifo_api.metrics.history > history/history.om
sudo docker compose run --rm --no-deps -T \
  -v "$PWD/history/history.om:/history.om:ro" \
  --entrypoint promtool prometheus \
  tsdb create-blocks-from openmetrics --max-block-duration=720h /history.om /prometheus \
  2>&1 | tail -1 | sed 's/^/  /'
# Reopened, so the new blocks are read now rather than at the next compaction.
sudo docker compose restart prometheus >/dev/null 2>&1
rm -f history/history.om
date -u +%FT%TZ > .history-imported
echo "  imported"
REMOTE
fi

# ---- 7. Checks -----------------------------------------------------------------------

say "Waiting for Prometheus and Grafana"
for _ in $(seq 1 40); do
  states="$("${SSH[@]}" "cd $REMOTE && sudo docker compose ps --format '{{.Service}} {{.Health}}'")"
  if grep -q '^prometheus healthy' <<<"$states" && grep -q '^grafana healthy' <<<"$states"; then
    break
  fi
  sleep 3
done
"${SSH[@]}" "cd $REMOTE && sudo docker compose ps --format '  {{.Name}}|{{.Status}}'"

say "What Prometheus is reading"
# Read once Prometheus has had a scrape interval to try every target.
sleep 5
"${SSH[@]}" "cd $REMOTE && sudo docker compose exec -T prometheus wget -qO- 'http://localhost:9090/api/v1/targets?state=active'" \
  | python3 -c '
import json, sys
for target in json.load(sys.stdin)["data"]["activeTargets"]:
    job = target["labels"]["job"]
    print("  %-10s %-8s %s" % (job, target["health"], target.get("lastError") or ""))
'

say "Checking it answers"
check() {
  local got
  got="$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "$1" || true)"
  if [[ "$got" == "$2" ]]; then
    echo "  $1 -> $got"
  else
    echo "  $1 -> $got, expected $2" >&2
    return 1
  fi
}
failed=0
check "$ORIGIN/" 200 || failed=1
check "$ORIGIN/api/v1/metrics" 404 || failed=1
# Not signed in from here, so the gate sends this to Eifo's sign-in.
check "$ORIGIN/grafana/" 302 || failed=1
[[ $failed == 0 ]] || die "Something answered wrongly; see above."

say "Done: $ORIGIN/grafana/ - sign in to Eifo as an administrator first."

#!/usr/bin/env bash
#
# Eifo installer.
#
#   curl -fsSL https://raw.githubusercontent.com/barakbl/eifo/main/install.sh | bash
#
# Downloads the latest tagged release (never main), checks what it needs,
# walks you through the keys and the services, sets up the database, and on
# macOS builds the menu-bar app and opens it. Everything it does is a command
# you could run by hand; it just asks the questions in order.

set -euo pipefail

REPO_SLUG="barakbl/eifo"
REPO_URL="${EIFO_INSTALL_REPO:-https://github.com/${REPO_SLUG}.git}"
PORT=3436
TMDB_URL="https://www.themoviedb.org/settings/api"
GOOGLE_URL="https://console.cloud.google.com/apis/credentials"

# ----------------------------------------------------------------- appearance
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
  B=$'\033[1m'; D=$'\033[2m'; U=$'\033[4m'; R=$'\033[0m'
  RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; CYN=$'\033[36m'; MAG=$'\033[35m'
else
  B=; D=; U=; R=; RED=; GRN=; YLW=; CYN=; MAG=
fi

BOX_W=54
# Built with ${out} braces on purpose: bash 3.2 (still /bin/bash on macOS)
# misparses "$out─" - a bare $var touching a multibyte char - under `set -u`.
_bar() {
  local i=0 out=""
  while [ "$i" -lt "$BOX_W" ]; do out="${out}─"; i=$((i + 1)); done
  printf '%s' "$out"
}
box() {
  local line pad
  printf '\n  %s╭%s╮%s\n' "$CYN" "$(_bar)" "$R"
  for line in "$@"; do
    pad=$((BOX_W - ${#line} - 1))
    if [ "$pad" -lt 0 ]; then pad=0; fi
    printf '  %s│%s %s%*s%s│%s\n' "$CYN" "$R" "$line" "$pad" "" "$CYN" "$R"
  done
  printf '  %s╰%s╯%s\n\n' "$CYN" "$(_bar)" "$R"
}

STEP=0
step()  { STEP=$((STEP + 1)); printf '\n  %s●%s  %s%s%s\n' "$MAG" "$R" "$B" "$1" "$R"; }
ok()    { printf '     %s✓%s %s\n' "$GRN" "$R" "$1"; }
note()  { printf '     %s%s%s\n' "$D" "$1" "$R"; }
warn()  { printf '     %s!%s %s\n' "$YLW" "$R" "$1"; }
die()   { printf '\n  %s✗%s %s\n\n' "$RED" "$R" "$1" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

trap 'printf "\n  %s✗%s installer stopped. Nothing was left running.\n\n" "$RED" "$R" >&2' ERR

# ---------------------------------------------------------------------- prompts
TTY=/dev/tty
ask() { # ask <var> <prompt> [default]
  local __v=$1 __p=$2 __d=${3:-} __a
  if [ -n "$__d" ]; then
    printf '     %s%s%s %s[%s]%s ' "$B" "$__p" "$R" "$D" "$__d" "$R" >"$TTY"
  else
    printf '     %s%s%s ' "$B" "$__p" "$R" >"$TTY"
  fi
  IFS= read -r __a <"$TTY" || __a=""
  if [ -z "$__a" ]; then __a=$__d; fi
  printf -v "$__v" '%s' "$__a"
}
confirm() { # confirm <prompt> <Y|N>   -> 0 = yes
  local __p=$1 __d=${2:-Y} __a __h
  case $__d in Y | y) __h="Y/n" ;; *) __h="y/N" ;; esac
  printf '     %s%s%s %s[%s]%s ' "$B" "$__p" "$R" "$D" "$__h" "$R" >"$TTY"
  IFS= read -r __a <"$TTY" || __a=""
  if [ -z "$__a" ]; then __a=$__d; fi
  case $__a in Y | y | yes | YES) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------------------ setup
usage() {
  cat <<EOF
${B}Eifo installer${R}

  curl -fsSL https://raw.githubusercontent.com/${REPO_SLUG}/main/install.sh | bash

Options:
  --dir PATH   set up an existing checkout at PATH instead of downloading
  --no-app     skip building and opening the macOS menu-bar app
  -h, --help   show this
EOF
}

SRC=""
NO_APP=0
while [ $# -gt 0 ]; do
  case $1 in
    --dir) SRC=${2:?--dir needs a path}; shift 2 ;;
    --dir=*) SRC=${1#*=}; shift ;;
    --no-app) NO_APP=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

if [ -z "${BASH_VERSION:-}" ]; then die "run this with bash, not sh."; fi
if ! { [ -r /dev/tty ] && [ -w /dev/tty ]; }; then
  die "the installer is interactive - run it in a terminal:
      bash <(curl -fsSL https://raw.githubusercontent.com/${REPO_SLUG}/main/install.sh)"
fi

case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux) OS=linux ;;
  *) OS=other ;;
esac

if [ -z "${EIFO_INSTALL_HANDOFF:-}" ]; then
  box "  Eifo installer" "" "  every Israeli streaming catalog, in one place"
fi

# --------------------------------------------------------------- 1. get the code
latest_tag() {
  git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null \
    | awk -F'\t' '{print $2}' | sed 's#refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1
}

MODE=""
if [ -n "$SRC" ]; then
  [ -f "$SRC/pyproject.toml" ] || die "$SRC does not look like an Eifo checkout"
  SRC=$(cd "$SRC" && pwd); MODE=inplace
elif [ -f ./pyproject.toml ] && grep -q '^name = "eifo"$' ./pyproject.toml 2>/dev/null; then
  step "Source"
  note "you are inside an Eifo checkout: $(pwd)"
  if confirm "Set up from this checkout?" Y; then SRC=$(pwd); MODE=inplace; fi
fi

if [ -z "$SRC" ]; then
  step "Downloading Eifo"
  have git || die "git is required."
  TAG=$(latest_tag || true)
  if [ -z "${TAG:-}" ]; then
    warn "no tagged release found; taking the main branch."
    TAG=main
  else
    ok "latest release is $TAG"
  fi
  DEST_DEFAULT="$HOME/eifo"
  ask DEST "Where should it live?" "$DEST_DEFAULT"
  case $DEST in "~"/*) DEST="$HOME/${DEST#~/}" ;; esac
  if [ -e "$DEST" ]; then
    if [ -f "$DEST/pyproject.toml" ] && grep -q '^name = "eifo"$' "$DEST/pyproject.toml" 2>/dev/null \
      && confirm "$DEST is already an Eifo checkout - use it?" Y; then
      SRC=$(cd "$DEST" && pwd); MODE=inplace
    else
      die "$DEST already exists - pick another location."
    fi
  fi
  if [ -z "$SRC" ]; then
    git clone --quiet -c advice.detachedHead=false --depth 1 \
      --branch "$TAG" "$REPO_URL" "$DEST"
    SRC=$(cd "$DEST" && pwd); MODE=clone
    ok "downloaded to $DEST"
  fi
fi

# Hand off to the copy of this script that shipped with the release, once, so
# the setup steps always match the code being installed.
if [ "$MODE" = clone ] && [ -z "${EIFO_INSTALL_HANDOFF:-}" ] && [ -f "$SRC/install.sh" ]; then
  export EIFO_INSTALL_HANDOFF=1
  HANDOFF=(--dir "$SRC")
  if [ "$NO_APP" = 1 ]; then HANDOFF+=(--no-app); fi
  exec bash "$SRC/install.sh" "${HANDOFF[@]}"
fi

cd "$SRC"

# ------------------------------------------------------------- 2. dependencies
step "Dependencies"

if have python3; then
  PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")
  case $PYV in
    3.1[2-9] | 3.[2-9][0-9]) ok "Python $PYV" ;;
    *) note "system Python is $PYV; uv will fetch a private 3.12 for this project" ;;
  esac
else
  note "no system Python; uv will fetch a private 3.12 for this project"
fi

if ! have uv; then
  warn "uv is not installed - it manages Python and every dependency"
  if confirm "Install uv now?" Y; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1091
    if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; fi
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    have uv || die "uv installed but not on PATH - open a new shell and re-run."
  else
    die "uv is required: https://docs.astral.sh/uv/"
  fi
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

cargo_ok() { command -v cargo >/dev/null 2>&1 && cargo --version >/dev/null 2>&1; }

BUILD_SIDECAR=0
if [ "$OS" = macos ] && [ "$NO_APP" = 0 ]; then
  BUILD_SIDECAR=1
  if ! cargo_ok; then
    warn "Rust is not ready - it is needed only to build the menu-bar app"
    if confirm "Set up Rust via rustup now?" Y; then
      if ! command -v rustup >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --quiet
      fi
      # shellcheck disable=SC1091
      if [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; fi
      export PATH="$HOME/.cargo/bin:$PATH"
      rustup default stable >/dev/null 2>&1 || true
    fi
    if ! cargo_ok; then
      BUILD_SIDECAR=0
      warn "no working Rust - skipping the menu-bar app"
      note "later:  rustup default stable  &&  ./sidecar/build-app.sh"
    fi
  fi
  if [ "$BUILD_SIDECAR" = 1 ]; then
    ok "Rust $(rustc --version 2>/dev/null | awk '{print $2}')"
  fi
fi

# --------------------------------------------------------- 3. python environment
step "Python environment"
note "installing dependencies - the first run can take a couple of minutes"
uv sync --quiet
ok "environment ready"

# ------------------------------------------------------------- 4. configuration
step "Configuration"

env_set() { # env_set <file> <key> <value>
  local f=$1 k=$2 v=$3 esc
  esc=$(printf '%s' "$v" | sed -e 's/[\\/&|]/\\&/g')
  if grep -q "^${k}=" "$f"; then
    # write through a sibling temp file rather than `sed -i` - BSD and GNU
    # disagree on `-i`, and `-i.bak` would trample the .env.bak kept above
    sed "s|^${k}=.*|${k}=${esc}|" "$f" >"$f.tmp"
    mv "$f.tmp" "$f"
  else
    printf '%s=%s\n' "$k" "$v" >>"$f"
  fi
}
gen_secret() {
  if have openssl; then openssl rand -hex 32
  else uv run --quiet python -c 'import secrets; print(secrets.token_urlsafe(48))'
  fi
}

WRITE_ENV=1
if [ -f .env ]; then
  if confirm ".env already exists - replace it?" N; then
    cp .env .env.bak; note "kept a copy at .env.bak"
  else
    WRITE_ENV=0; note "keeping your .env"
  fi
fi

TMDB_KEY=""; GOOGLE_ID=""; GOOGLE_SECRET=""; ADMIN_EMAIL=""; SECRET_KEY=""
if [ "$WRITE_ENV" = 1 ]; then
  printf '\n'
  note "TMDB gives you ratings, posters, and availability for the big services."
  note "A free key takes about a minute:  ${U}${TMDB_URL}${R}${D}"
  ask TMDB_KEY "TMDB API key (blank to skip)" ""
  if [ -z "$TMDB_KEY" ]; then
    warn "without it, the subscription services and all ratings stay empty"
  fi

  printf '\n'
  if confirm "Set up Google sign-in? (optional - enables personal lists and the Manage tab)" N; then
    note "Create an OAuth client, type 'Web application':  ${U}${GOOGLE_URL}${R}${D}"
    note "Authorised redirect URI:"
    note "  http://localhost:${PORT}/api/v1/auth/callback/google"
    ask GOOGLE_ID "Client ID" ""
    ask GOOGLE_SECRET "Client secret" ""
    ask ADMIN_EMAIL "Your Google account email (becomes the admin; blank for none)" ""
    SECRET_KEY=$(gen_secret)
  fi

  cp .env.example .env
  env_set .env EIFO_TMDB_API_KEY "$TMDB_KEY"
  if [ -n "$GOOGLE_ID$GOOGLE_SECRET" ]; then
    env_set .env EIFO_SECRET_KEY "$SECRET_KEY"
    env_set .env EIFO_GOOGLE_CLIENT_ID "$GOOGLE_ID"
    env_set .env EIFO_GOOGLE_CLIENT_SECRET "$GOOGLE_SECRET"
    if [ -n "$ADMIN_EMAIL" ]; then
      printf 'EIFO_ADMIN_EMAILS=%s\n' "$ADMIN_EMAIL" >>.env
    fi
  fi
  ok "wrote .env"
fi

# --- services ---------------------------------------------------------------
SOURCES=()
while IFS= read -r key; do SOURCES+=("$key"); done < <(
  grep -oE '^\[sources\.[a-z0-9_]+\]' config/eifo.example.toml \
    | sed -E 's/.*\.([a-z0-9_]+)\]/\1/' | grep -vx now14
)

pretty() {
  case $1 in
    netflix_il) echo "Netflix" ;;
    prime_video_il) echo "Prime Video" ;;
    apple_tv_plus) echo "Apple TV+" ;;
    apple_tv_store) echo "Apple TV store (rent & buy - adds ~18 min per run)" ;;
    hbo_max_il) echo "HBO Max" ;;
    mubi_il) echo "MUBI" ;;
    crunchyroll_il) echo "Crunchyroll (anime)" ;;
    disney_plus_il) echo "Disney+" ;;
    freetv) echo "FreeTV" ;;
    mako) echo "Mako VOD (Keshet 12)" ;;
    kan) echo "Kan Box (Kan 11) - needs a headless browser" ;;
    reshet13) echo "Reshet 13 - needs a headless browser" ;;
    cinematheque_vod) echo "Cinematheque VOD (Tel Aviv)" ;;
    israel_film_archive) echo "Israel Film Archive (Jerusalem)" ;;
    *) echo "$1" ;;
  esac
}

printf '\n'
note "${#SOURCES[@]} services are supported."
printf '     %s1%s  Everything  %s(recommended)%s\n' "$B" "$R" "$D" "$R"
printf '     %s2%s  Let me choose\n' "$B" "$R"
ask SVC_CHOICE "Which?" "1"

SELECTED=" ${SOURCES[*]} "
if [ "$SVC_CHOICE" = 2 ]; then
  printf '\n'
  while :; do
    i=1
    for key in "${SOURCES[@]}"; do
      case $SELECTED in
        *" $key "*) printf '     %s%2d%s  %s[x]%s  %s\n' "$D" "$i" "$R" "$GRN" "$R" "$(pretty "$key")" ;;
        *) printf '     %s%2d%s  [ ]  %s%s%s\n' "$D" "$i" "$R" "$D" "$(pretty "$key")" "$R" ;;
      esac
      i=$((i + 1))
    done
    ask TOGGLE "Numbers to toggle (e.g. \"3 7\"), \"all\", \"none\", or ⏎ to accept" ""
    if [ -z "$TOGGLE" ]; then break; fi
    case $TOGGLE in
      all) SELECTED=" ${SOURCES[*]} " ;;
      none) SELECTED=" " ;;
      *)
        for n in $TOGGLE; do
          case $n in '' | *[!0-9]*) continue ;; esac
          if [ "$n" -ge 1 ] && [ "$n" -le "${#SOURCES[@]}" ]; then
            key=${SOURCES[$((n - 1))]}
            case $SELECTED in
              *" $key "*) SELECTED=${SELECTED/" $key "/" "} ;;
              *) SELECTED="$SELECTED$key " ;;
            esac
          fi
        done ;;
    esac
    printf '\n'
  done
fi

WRITE_CFG=1
if [ -f config/eifo.toml ]; then
  if confirm "config/eifo.toml already exists - replace it?" N; then
    cp config/eifo.toml config/eifo.toml.bak; note "kept a copy at config/eifo.toml.bak"
  else
    WRITE_CFG=0; note "keeping your config/eifo.toml"
  fi
fi

if [ "$WRITE_CFG" = 1 ]; then
  awk -v en="$SELECTED" '
    /^\[sources\.[a-z0-9_]+\]/ { cur=$0; sub(/^\[sources\./,"",cur); sub(/\]$/,"",cur); print; next }
    /^\[/ { cur=""; print; next }
    {
      if (cur != "" && $0 ~ /^enabled[ \t]*=/) {
        print (index(en, " " cur " ") ? "enabled = true" : "enabled = false"); next
      }
      print
    }
  ' config/eifo.example.toml >config/eifo.toml
  COUNT=$(printf '%s' "$SELECTED" | wc -w | tr -d ' ')
  ok "wrote config/eifo.toml - $COUNT service(s) enabled"
fi

NEEDS_BROWSER=0
case $SELECTED in *" kan "* | *" reshet13 "*) NEEDS_BROWSER=1 ;; esac

# ---------------------------------------------------------------- 5. database
step "Database"
uv run --quiet eifo-fetch db upgrade
ok "schema created at data/eifo.db"

# ------------------------------------------------------- 6. headless browser
if [ "$NEEDS_BROWSER" = 1 ]; then
  step "Headless browser"
  note "Kan and Reshet 13 block non-browser clients, so they read their pages"
  note "through a headless Chromium (about 150 MB)."
  if confirm "Install it now?" Y; then
    uv run --quiet playwright install chromium
    if [ "$OS" = linux ]; then
      note "on Linux you may also need: uv run playwright install-deps chromium"
    fi
    ok "Chromium installed"
  else
    warn "Kan and Reshet 13 will fail until: uv run playwright install chromium"
  fi
fi

# --------------------------------------------------------- 7. first catalog fill
step "First catalog fill"
note "Pulls every enabled service. Twenty minutes to an hour, depending on which."
if confirm "Run it now?" N; then
  uv run eifo-fetch all || warn "the fill hit errors - re-run 'uv run eifo-fetch all' later"
else
  note "later:  cd $SRC && uv run eifo-fetch all"
fi

# ------------------------------------------------------------ 8. how it runs
APP_OPENED=0
if [ "$BUILD_SIDECAR" = 1 ]; then
  step "Menu-bar app"
  # A build failure here is not fatal - the rest of Eifo is already working.
  if ./sidecar/build-app.sh >/dev/null; then
    APP="$SRC/sidecar/target/Eifo.app"
    CFG_DIR="$HOME/Library/Application Support/Eifo"
    if [ ! -f "$CFG_DIR/config.json" ]; then
      mkdir -p "$CFG_DIR"
      esc=$(printf '%s' "$SRC" | sed -e 's/[\\"]/\\&/g')
      printf '{\n  "app_dir": "%s"\n}\n' "$esc" >"$CFG_DIR/config.json"
      ok "pointed the app at $SRC"
    fi
    if open "$APP"; then APP_OPENED=1; fi
    ok "Eifo is in your menu bar (top right) - it starts the web server itself"
  else
    warn "the menu-bar app did not build - the rest of Eifo is fine"
    note "try again later:  cd $SRC && ./sidecar/build-app.sh && open sidecar/target/Eifo.app"
  fi
fi

# ------------------------------------------------------------------ 9. done
LOC=$SRC
# shellcheck disable=SC2088  # the ~ is for display, not for expansion
case $LOC in "$HOME"/*) LOC="~/${LOC#"$HOME"/}" ;; esac

box \
  "  Eifo is installed" \
  "" \
  "  web      http://localhost:${PORT}" \
  "  manage   http://localhost:${PORT}/#/manage"

note "folder   $LOC"
if [ "$APP_OPENED" = 1 ]; then
  note "the menu-bar dot runs the server and the nightly refresh - click it"
  note "or fill the catalog now:  cd $LOC && uv run eifo-fetch all"
else
  note "start it:      cd $LOC && uv run eifo-api"
  note "fill it:       cd $LOC && uv run eifo-fetch all"
  note "keep it fresh: cd $LOC && uv run eifo-fetch daemon   (or a nightly cron line)"
fi
printf '\n'

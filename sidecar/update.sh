#!/usr/bin/env bash
#
# Move an Eifo checkout to a newer release, and rebuild the menu-bar app.
#
#   update.sh <checkout-dir> <tag>
#
# Every line is a command a person updating by hand would run: fetch the tag,
# check it out, re-sync the Python environment, apply migrations, rebuild
# Eifo.app. The menu-bar app runs this, then relaunches itself from the bundle
# it leaves behind.
set -euo pipefail

dir=${1:?checkout directory}
tag=${2:?release tag}

# A GUI app launched from Finder has almost nothing on PATH; put the tools the
# installer relies on back onto it.
# shellcheck disable=SC1091
if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; fi
# shellcheck disable=SC1091
if [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; fi
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

cd "$dir"

git fetch --depth 1 origin "refs/tags/${tag}:refs/tags/${tag}"
git checkout -q "$tag"

uv sync --quiet
.venv/bin/eifo-fetch db upgrade

# Build the new bundle beside the running one and swap it in only once it is
# whole, so a failure partway through never leaves nothing to relaunch.
./sidecar/build-app.sh "$dir/sidecar/target/Eifo.next.app"
rm -rf "$dir/sidecar/target/Eifo.app"
mv "$dir/sidecar/target/Eifo.next.app" "$dir/sidecar/target/Eifo.app"

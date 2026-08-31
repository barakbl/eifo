#!/usr/bin/env bash
# Build Eifo.app - the bundle, not just the binary.
#
# A menu-bar app has to be a bundle: LSUIElement lives in Info.plist, and
# SMAppService identifies a login item by its bundle, so a bare binary has
# nothing to register. `cargo run` still works for development, which is why
# the activation policy is also set in code.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${here}/target/release/eifo-tray"
app="${1:-${here}/target/Eifo.app}"

cargo build --release --manifest-path "${here}/Cargo.toml"

rm -rf "${app}"
mkdir -p "${app}/Contents/MacOS" "${app}/Contents/Resources"
cp "${target}" "${app}/Contents/MacOS/eifo-tray"
cp "${here}/resources/Info.plist" "${app}/Contents/Info.plist"

# Ad-hoc signature. Unsigned bundles are refused by SMAppService, so without
# this the "Open at login" toggle fails with an error rather than doing nothing.
codesign --force --sign - "${app}" >/dev/null 2>&1 || {
    echo "note: could not codesign; 'Open at login' will not work" >&2
}

echo "built ${app}"
echo "run it with: open '${app}'"

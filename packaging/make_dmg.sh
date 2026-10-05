#!/bin/bash
# Package dist/FinOrganizer.app into a drag-to-install disk image.
#
#   packaging/make_dmg.sh dist/FinOrganizer-mac.dmg
#
# The app is ad-hoc signed (no Apple Developer ID), so the first launch needs
# System Settings > Privacy & Security > "Open Anyway"; see README.
set -euo pipefail

OUT="${1:-dist/FinOrganizer.dmg}"
APP="dist/FinOrganizer.app"
[ -d "$APP" ] || { echo "missing $APP; run packaging/build_exe.py first" >&2; exit 1; }

# Re-sign the whole bundle consistently (Apple Silicon refuses to run unsigned code).
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # drag the app onto this to install

rm -f "$OUT"
hdiutil create -volname "FinOrganizer" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
hdiutil verify "$OUT"
echo "Created $OUT"

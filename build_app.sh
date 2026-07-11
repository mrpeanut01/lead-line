#!/bin/sh
# Build, sign, and package LeadLine.app (requires: .venv with requirements + pyinstaller)
# Version comes from leadline/__init__.py via LeadLine.spec.
#
# Signing happens in a temp dir outside the project tree: cloud-synced folders
# (iCloud Drive / OneDrive) stamp com.apple.FinderInfo xattrs onto files, which
# codesign rejects as "detritus". The release zip is created from the clean
# copy before it re-enters the synced tree.
set -e
cd "$(dirname "$0")"
.venv/bin/pyinstaller --noconfirm --clean LeadLine.spec

VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" dist/LeadLine.app/Contents/Info.plist)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ditto --norsrc --noextattr --noacl dist/LeadLine.app "$TMP/LeadLine.app"
codesign --force --deep --sign - "$TMP/LeadLine.app"
codesign --verify --deep --strict "$TMP/LeadLine.app"
ditto -c -k --norsrc --noextattr --noacl --keepParent "$TMP/LeadLine.app" "$TMP/LeadLine-$VERSION.zip"

rm -rf dist/LeadLine.app
ditto --norsrc --noextattr --noacl "$TMP/LeadLine.app" dist/LeadLine.app
cp "$TMP/LeadLine-$VERSION.zip" dist/

echo "Built and signed: dist/LeadLine.app + dist/LeadLine-$VERSION.zip (v$VERSION)"

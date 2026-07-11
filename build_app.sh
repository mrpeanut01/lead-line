#!/bin/sh
# Build LeadLine.app into dist/ (requires: .venv with requirements + pyinstaller)
set -e
cd "$(dirname "$0")"
.venv/bin/pyinstaller --noconfirm --clean --windowed \
  --name LeadLine \
  --osx-bundle-identifier io.github.mrpeanut01.leadline \
  --add-data "leadline/ui:leadline/ui" \
  run_leadline.py
echo "Built dist/LeadLine.app"

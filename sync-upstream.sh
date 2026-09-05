#!/bin/bash
# Sync your fork with upstream TauricResearch/TradingAgents
# Run this periodically to pull in the latest changes.
#
# Usage: ./sync-upstream.sh
#
# What it does:
#   1. Fetches latest from upstream (TauricResearch)
#   2. Fast-forwards your local main to match upstream/main
#   3. Rebases your indian-market branch on top of updated main
#   4. Pushes the updated main to your fork

set -euo pipefail

echo "==> Fetching upstream..."
git fetch upstream

echo "==> Updating local main to match upstream/main..."
git checkout main
git merge --ff-only upstream/main

echo "==> Pushing updated main to your fork..."
git push origin main

echo "==> Rebasing indian-market on top of updated main..."
git checkout indian-market
git rebase main

echo "==> Pushing updated indian-market to your fork..."
git push origin indian-market --force-with-lease

echo ""
echo "Done! Your branches are now in sync with upstream."
echo "  main:            $(git log -1 --format='%h %s' main)"
echo "  indian-market:   $(git log -1 --format='%h %s' indian-market)"

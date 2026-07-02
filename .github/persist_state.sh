#!/usr/bin/env bash
# Commit the bot's living state back to the repo so the NEXT scheduled run
# continues from it (GitHub runners are wiped after every job). The reports/
# folder is gitignored, so we force-add only the data files we want to keep.
set -u
label="${1:-run}"

git config user.name  "trading-bot"
git config user.email "trading-bot@users.noreply.github.com"

# Force-add the state files ONE AT A TIME. (A single `git add a b c` aborts
# entirely if any one path is missing, so a missing learning.json used to
# silently prevent EVERYTHING - locks, paper book, dashboard - from saving.)
for p in \
  reports/paper_trades.csv \
  reports/history.csv \
  reports/learning.json \
  reports/learning_log.csv \
  reports/locked \
  reports/peaks \
  docs
do
  git add -f "$p" 2>/dev/null || true
done

if git diff --cached --quiet; then
  echo "No state changes to save."
  exit 0
fi

git commit -m "state: ${label} $(date -u +%FT%TZ) [skip ci]"
# Re-sync in case a sibling job pushed while we ran, then push.
git pull --rebase --autostash || true
git push || echo "push failed (will reconcile next run)"

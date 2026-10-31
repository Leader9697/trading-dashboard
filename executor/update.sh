#!/bin/sh
# Daily self-update on the VPS (run by the executor-v3-update timer, see README).
# Pulls main from GitHub only when there is something new, runs the executor's tests on it,
# and restarts the executor only if they pass. A failed test keeps the old version running.
set -u
cd "$(dirname "$0")/.." || exit 1
git fetch -q origin main || exit 1
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && exit 0
OLD=$(git rev-parse HEAD)
git merge -q --ff-only origin/main || exit 1
if python3 -m pytest -q executor/test_executor.py; then
    systemctl restart executor-v3
    echo "updated $OLD -> $(git rev-parse HEAD)"
else
    git reset -q --hard "$OLD"
    python3 executor/executor.py notify "⚠️ Update eksekutor dibatalkan: tes gagal. Versi lama tetap jalan."
    exit 1
fi

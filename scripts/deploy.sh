#!/usr/bin/env bash
# Put main on the production server. Run from the workstation; drives the server by ssh.
#
# Deploying is deliberately separate from merging. main can sit ahead of production for a
# while, and going live is a decision somebody makes rather than a side effect of a merge.
#
#   bash scripts/deploy.sh              # deploy main
#   bash scripts/deploy.sh --dry-run    # show what would go out, change nothing

set -uo pipefail
cd "$(dirname "$0")/.."

HOST="${PLUMBING_HOST:-root@64.118.150.41}"
REMOTE="${PLUMBING_REMOTE_DIR:-/root/plumbing}"
SERVICE="plumbing-inbound"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

refuse() { echo "REFUSING: $1" >&2; exit 1; }

# ---- refuse to deploy something that is not main, or not pushed ------
BRANCH="$(git branch --show-current)"
[ "$BRANCH" = "main" ] || refuse "you are on '$BRANCH'. Production runs main. Merge first."

git fetch -q origin main
LOCAL="$(git rev-parse HEAD)"
REMOTE_MAIN="$(git rev-parse origin/main)"
[ "$LOCAL" = "$REMOTE_MAIN" ] || refuse "local main and origin/main differ. Push first — the server pulls from origin, not from here."

[ -z "$(git status --porcelain)" ] || refuse "you have uncommitted changes. They would not be deployed, which makes what you tested and what ships different things."

# ---- refuse if the server has drifted --------------------------------
DIRTY="$(ssh -o ConnectTimeout=20 "$HOST" "cd $REMOTE && git status --porcelain --untracked-files=no" 2>/dev/null)"
if [ -n "$DIRTY" ]; then
  echo "The server has uncommitted changes to tracked files:" >&2
  echo "$DIRTY" | sed 's/^/    /' >&2
  refuse "a pull would conflict with these, or silently discard them. Deployment state belongs in the systemd unit, not in tracked files."
fi

CURRENT="$(ssh -o ConnectTimeout=20 "$HOST" "cd $REMOTE && git rev-parse HEAD" 2>/dev/null)"
echo "server is at : ${CURRENT:0:7}"
echo "deploying    : ${LOCAL:0:7}"

if [ "$CURRENT" = "$LOCAL" ]; then
  echo "Already up to date. Nothing to do."
  exit 0
fi

echo
echo "--- going out ---"
git log --oneline "$CURRENT".."$LOCAL" 2>/dev/null | sed 's/^/    /' || echo "    (cannot list; the server may be on an unrelated commit)"

if [ "$DRY_RUN" = "1" ]; then
  echo; echo "Dry run. Nothing changed."
  exit 0
fi

# ---- deploy ----------------------------------------------------------
echo
ssh -o ConnectTimeout=60 "$HOST" "
  set -e
  cd $REMOTE
  git pull -q origin main
  # Before the restart, so a dependency a commit needs is there when it starts rather
  # than discovered at the moment an agent tries to use it. Quiet unless something
  # actually installs; it is a no-op on almost every deploy.
  .venv/bin/pip install -q -r requirements.txt
  systemctl restart $SERVICE
  sleep 3
  systemctl is-active $SERVICE
" || refuse "the deploy failed. The server may be mid-way; check it before retrying."

echo
echo "--- health ---"
for _ in 1 2 3; do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://smartstrategy.services/health || echo 000)"
  [ "$CODE" = "200" ] && break
  sleep 3
done
echo "  https://smartstrategy.services/health -> HTTP $CODE"

echo
echo "--- what is actually reaching the outside world ---"
ssh -o ConnectTimeout=20 "$HOST" "cd $REMOTE && PYTHONPATH=src .venv/bin/python -c \"
from plumbing.integrations.gate import live_status
s = live_status()
print('  master:', s['master_switch'], '(' + s['master_switch_source'] + ')')
print('  live  :', s['effectively_live'] or 'nothing', '(' + s['tools_source'] + ')')
\"" 2>/dev/null

if [ "$CODE" != "200" ]; then
  echo
  echo "HEALTH CHECK FAILED. Roll back with:"
  echo "  ssh $HOST 'cd $REMOTE && git checkout ${CURRENT:0:7} && systemctl restart $SERVICE'"
  exit 1
fi

echo
echo "Deployed. To roll back:"
echo "  ssh $HOST 'cd $REMOTE && git checkout ${CURRENT:0:7} && systemctl restart $SERVICE'"

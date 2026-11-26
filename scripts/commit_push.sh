#!/usr/bin/env bash
# Commit the given paths (only if they changed) and push, retrying on races.
#
#   scripts/commit_push.sh <message-file> "<regenerate command>" <path>...
#
# On a rejected push: 'git pull --rebase'; if the rebase conflicts (e.g. two runs
# rewrote the same daily summary), fall back to a hard reset onto origin and run
# the regenerate command again, so generated files are rebuilt on top of the
# newest main. The regenerate command must be idempotent.
set -euo pipefail

msg_file="$1"; regen="$2"; shift 2
branch="${TARGET_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for attempt in 1 2 3 4 5 6; do
  git add -A -- "$@"
  if git diff --cached --quiet; then
    echo "nothing to commit"
    exit 0
  fi
  git commit -q -F "$msg_file"
  if git push -q origin "HEAD:${branch}"; then
    echo "pushed on attempt ${attempt}"
    exit 0
  fi
  echo "push rejected (attempt ${attempt}), syncing with origin/${branch}"
  git fetch -q origin "${branch}"
  if ! git rebase -q "origin/${branch}"; then
    git rebase --abort || true
    git reset -q --hard "origin/${branch}"
    bash -c "${regen}"
  elif ! git push -q origin "HEAD:${branch}"; then
    git reset -q --hard "origin/${branch}"
    bash -c "${regen}"
  else
    echo "pushed after rebase"
    exit 0
  fi
  sleep $((attempt * 4))
done
echo "giving up after 6 attempts" >&2
exit 1

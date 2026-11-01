#!/usr/bin/env bash
# Pull or push runtime state on the dedicated state branch.
#   scripts/state_sync.sh pull
#   scripts/state_sync.sh push
# Dry-runs and local shells do not push. The token is never read or printed.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
BRANCH="${STATE_BRANCH:-cursor/csnews-state-a760}"
FILES=(
  sent_log.json
  state.json
  auto_state.json
  live_queue.json
  collector_state.json
)

# daily/YYYY-MM-DD.md is the per-day channel digest. It is gitignored on main
# (so a local run cannot land it on the code branch) and force-added here.
prune_daily_dir() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  python3 - "$dir" <<'PY'
import sys
import daily_digest
for name in daily_digest.prune(sys.argv[1]):
    print(f"state: pruned daily/{name}")
PY
}

copy_daily() {
  mkdir -p "$STATE_TMP/daily"
  if [[ -d daily ]]; then
    local f
    for f in daily/*.md; do
      [[ -e "$f" ]] || continue
      cp "$f" "$STATE_TMP/daily/"
    done
  fi
  prune_daily_dir "$STATE_TMP/daily"
}

stage_daily() {
  local f base
  for f in "$STATE_TMP"/daily/*.md; do
    [[ -e "$f" ]] || continue
    base=$(basename "$f")
    git -C "$STATE_TMP" add -f -- "daily/$base"
  done
  # `git add -u` exits non-zero when daily/ has never been committed. Deletions
  # from prune still need to be staged once the path is tracked.
  if git -C "$STATE_TMP" ls-files -- daily | grep -q .; then
    git -C "$STATE_TMP" add -u -- daily
  fi
}

pull() {
  if git fetch --depth=20 origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"; then
    git checkout "origin/${BRANCH}" -- "${FILES[@]}"
    if git cat-file -e "origin/${BRANCH}:daily" 2>/dev/null; then
      git checkout "origin/${BRANCH}" -- daily
    fi
    echo "state: pulled ${BRANCH}"
  else
    echo "state: branch ${BRANCH} is not on origin yet; using the copies in this checkout"
  fi
}

# Worktree path must be global. A `local` here is gone when the EXIT trap runs
# after push() returns, and `set -u` then aborts the job with "tmp: unbound variable"
# (the 23:24 MSK run on 17 Oct died this way, after the state commit had succeeded).
STATE_TMP=""
cleanup_state_tmp() {
  if [[ -z "${STATE_TMP:-}" || ! -d "${STATE_TMP}" ]]; then
    return 0
  fi
  git worktree remove --force "$STATE_TMP" >/dev/null 2>&1 || rm -rf "$STATE_TMP"
  STATE_TMP=""
}

push() {
  if [[ "${GHA_DRY_RUN:-}" == "true" || "${DRY_RUN:-}" == "1" ]]; then
    echo "state: dry-run, not committing"
    return 0
  fi
  if [[ -z "${GITHUB_ACTIONS:-}" ]]; then
    echo "state: not on Actions, not committing"
    return 0
  fi
  STATE_TMP=$(mktemp -d)
  trap cleanup_state_tmp EXIT
  if git fetch --depth=20 origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"; then
    git worktree add --detach "$STATE_TMP" "origin/${BRANCH}"
  else
    echo "state: creating ${BRANCH} from this commit"
    git worktree add --detach "$STATE_TMP" HEAD
  fi
  local f
  for f in "${FILES[@]}"; do
    cp "$f" "$STATE_TMP/$f"
  done
  copy_daily
  git -C "$STATE_TMP" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git -C "$STATE_TMP" config user.name "github-actions[bot]"
  git -C "$STATE_TMP" add -- "${FILES[@]}"
  stage_daily
  if git -C "$STATE_TMP" diff --cached --quiet; then
    echo "state: unchanged"
    return 0
  fi
  git -C "$STATE_TMP" commit -m "state: ${GHA_JOB:-${GHA_MODE:-${GITHUB_EVENT_SCHEDULE:-run}}} $(date -u +%Y-%m-%dT%H:%MZ)"
  local i
  for i in 1 2 3 4 5; do
    if git -C "$STATE_TMP" push origin "HEAD:${BRANCH}"; then
      echo "state: pushed ${BRANCH}"
      return 0
    fi
    echo "state: push failed (try ${i}), refetching"
    sleep $((i * 3))
    if git -C "$STATE_TMP" fetch --depth=20 origin "${BRANCH}"; then
      git -C "$STATE_TMP" rebase FETCH_HEAD
    fi
  done
  echo "state: push failed after retries" >&2
  return 1
}

case "${1:-}" in
  pull) pull ;;
  push) push ;;
  *) echo "usage: $0 pull|push" >&2; exit 2 ;;
esac

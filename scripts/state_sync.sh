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

pull() {
  if git fetch --depth=20 origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"; then
    git checkout "origin/${BRANCH}" -- "${FILES[@]}"
    echo "state: pulled ${BRANCH}"
  else
    echo "state: branch ${BRANCH} is not on origin yet; using the copies in this checkout"
  fi
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
  local tmp
  tmp=$(mktemp -d)
  cleanup() { git worktree remove --force "$tmp" >/dev/null 2>&1 || rm -rf "$tmp"; }
  trap cleanup EXIT
  if git fetch --depth=20 origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"; then
    git worktree add --detach "$tmp" "origin/${BRANCH}"
  else
    echo "state: creating ${BRANCH} from this commit"
    git worktree add --detach "$tmp" HEAD
  fi
  local f
  for f in "${FILES[@]}"; do
    cp "$f" "$tmp/$f"
  done
  git -C "$tmp" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git -C "$tmp" config user.name "github-actions[bot]"
  git -C "$tmp" add -- "${FILES[@]}"
  if git -C "$tmp" diff --cached --quiet; then
    echo "state: unchanged"
    return 0
  fi
  git -C "$tmp" commit -m "state: ${GHA_MODE:-${GITHUB_EVENT_SCHEDULE:-run}} $(date -u +%Y-%m-%dT%H:%MZ)"
  local i
  for i in 1 2 3 4 5; do
    if git -C "$tmp" push origin "HEAD:${BRANCH}"; then
      echo "state: pushed ${BRANCH}"
      return 0
    fi
    echo "state: push failed (try ${i}), refetching"
    sleep $((i * 3))
    if git -C "$tmp" fetch --depth=20 origin "${BRANCH}"; then
      git -C "$tmp" rebase FETCH_HEAD
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

#!/usr/bin/env bash
# merge_if_clean.sh — merge controlado de branch para main
# Uso: merge_if_clean.sh <branch>
# Exit 0 = MERGE_OK, Exit 10 = worktree dirty, Exit 1 = merge falhou
set -euo pipefail

AIOS_ROOT="${AIOS_ROOT:-$HOME/ai-os}"
cd "$AIOS_ROOT"

BR="${1:?branch required}"

if [ -n "$(git status --porcelain)" ]; then
  echo "WORKTREE_DIRTY"
  exit 10
fi

CURRENT=$(git rev-parse --abbrev-ref HEAD)

git checkout main
git pull --ff-only 2>/dev/null || true

if ! git merge --no-ff "$BR" -m "aios: autopilot merge $BR"; then
  echo "MERGE_CONFLICT"
  git merge --abort 2>/dev/null || true
  git checkout "$CURRENT" 2>/dev/null || true
  exit 1
fi

echo "MERGE_OK"

#!/usr/bin/env bash
# The single source of truth for "is the build green?".
# Run by the agents, and independently by the bot after every build round.
# Sections are skipped until the corresponding folder exists.
set -uo pipefail
cd "$(dirname "$0")/.."

FAILED=()
run() {  # run <name> <command...>
  local name="$1"; shift
  echo "::: $name"
  if "$@"; then echo "::: $name OK"; else echo "::: $name FAILED"; FAILED+=("$name"); fi
}

# ---------- Backend ----------
if [ -f backend/manage.py ]; then
  if [ -f backend/requirements.txt ]; then
    run "backend: install dependencies" bash -c "cd backend && pip install -q -r requirements.txt"
  fi
  run "backend: lint"        bash -c "cd backend && ruff check ."
  run "backend: migrations"  bash -c "cd backend && python manage.py makemigrations --check --dry-run"
  run "backend: tests"       bash -c "cd backend && pytest -q"
else
  echo "::: backend: skipped (no backend/manage.py yet)"
fi

# ---------- Frontend ----------
if [ -f frontend/angular.json ]; then
  [ -d frontend/node_modules ] || (cd frontend && npm ci --no-audit --no-fund)
  run "frontend: build"  bash -c "cd frontend && npx ng build"
  run "frontend: tests"  bash -c "cd frontend && CI=1 npx ng test --watch=false"
else
  echo "::: frontend: skipped (no frontend/angular.json yet)"
fi

# ---------- End-to-end ----------
if [ -f e2e/playwright.config.ts ] && [ "${SKIP_E2E:-0}" != "1" ]; then
  [ -d e2e/node_modules ] || (cd e2e && npm ci --no-audit --no-fund)
  run "e2e: playwright"  bash -c "cd e2e && npx playwright test --reporter=line"
else
  echo "::: e2e: skipped"
fi

echo
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "VERIFY: PASS"
  exit 0
else
  echo "VERIFY: FAIL (${FAILED[*]})"
  exit 1
fi

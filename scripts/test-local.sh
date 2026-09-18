#!/usr/bin/env bash
# Run the same checks as CI locally with Docker (WSL or any Linux Docker host).
#
#   scripts/test-local.sh              lint + unit + integration (everything CI runs before publishing)
#   scripts/test-local.sh lint         ruff, mypy, bandit
#   scripts/test-local.sh unit         unit tests inside the runtime image (Linux, poppler, non-root)
#   scripts/test-local.sh integration  end-to-end test against a real qBittorrent
#   scripts/test-local.sh jellyfin     end-to-end test against real qBittorrent + Jellyfin 12 (slower;
#                                      JELLYFIN_IMAGE=jellyfin/jellyfin:12.0 scripts/test-local.sh jellyfin for another version)
#   scripts/test-local.sh ui           start Periodica + qBittorrent with sample newspapers for manual testing
#   scripts/test-local.sh down         stop the ui/integration containers and remove their volumes
set -euo pipefail
cd "$(dirname "$0")/.."

compose=(docker compose -f tests/integration/compose.yml)

lint() {
  echo "== lint (ruff, mypy, bandit)"
  docker build --target lint --no-cache-filter lint --progress plain -t periodica:lint .
}

unit() {
  echo "== unit tests"
  docker build --target test --no-cache-filter test --progress plain -t periodica:test .
}

integration() {
  echo "== integration test against a real qBittorrent"
  "${compose[@]}" --profile ui --profile jellyfin down -v --remove-orphans >/dev/null 2>&1 || true
  "${compose[@]}" up -d qbittorrent
  local rc=0
  "${compose[@]}" run --rm --build runner || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "-- qBittorrent log (last 50 lines)"
    "${compose[@]}" logs --tail 50 qbittorrent || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  return "$rc"
}

jellyfin() {
  echo "== Jellyfin integration test (qBittorrent + Jellyfin 12)"
  "${compose[@]}" --profile ui --profile jellyfin down -v --remove-orphans >/dev/null 2>&1 || true
  "${compose[@]}" --profile jellyfin up -d qbittorrent jellyfin
  local rc=0
  "${compose[@]}" --profile jellyfin run --rm --build -e NL_IT_JELLYFIN_URL=http://jellyfin:8096 runner     python -m pytest -q -rs -p no:cacheprovider tests/integration/test_jellyfin_it.py || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "-- Jellyfin log (last 80 lines)"
    "${compose[@]}" --profile jellyfin logs --tail 80 jellyfin || true
  fi
  "${compose[@]}" --profile jellyfin down -v --remove-orphans >/dev/null 2>&1 || true
  return "$rc"
}

ui() {
  echo "== starting Periodica + qBittorrent with sample newspapers"
  "${compose[@]}" --profile ui down -v --remove-orphans >/dev/null 2>&1 || true
  "${compose[@]}" --profile ui up -d --build qbittorrent app
  "${compose[@]}" run --rm --build runner python -m tests.integration.seed
  cat <<'EOF'

Periodica:  http://localhost:8765   (create any admin account on first visit; settings are pre-filled)
qBittorrent:  http://localhost:18080  (admin / integration-only-password)

Stop and remove everything with:  scripts/test-local.sh down
EOF
}

down() {
  "${compose[@]}" --profile ui --profile jellyfin down -v --remove-orphans
}

case "${1:-all}" in
  lint) lint ;;
  unit) unit ;;
  integration) integration ;;
  jellyfin) jellyfin ;;
  ui) ui ;;
  down) down ;;
  all) lint && unit && integration && echo "== all checks passed" ;;
  *) echo "usage: $0 [all|lint|unit|integration|jellyfin|ui|down]" >&2; exit 2 ;;
esac

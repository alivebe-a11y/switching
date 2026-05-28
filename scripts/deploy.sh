#!/usr/bin/env bash
#
# deploy.sh — one-shot deploy for the Switching stack on TrueNAS / Dockge.
#
# All services share ONE image (ghcr.io/alivebe-a11y/switching:latest) and the
# same GitHub build context, so we build the image ONCE then recreate every
# active service onto it. A running container keeps its old image until it is
# recreated, so restarting only one service would leave the others on stale code.
#
# Usage (run from the Dockge stack directory, e.g.
#   /Pool_1/Configs/dockge2/Stacks/stocks):
#
#   # Easiest — pipe straight from GitHub:
#   curl -sL https://raw.githubusercontent.com/alivebe-a11y/switching/main/scripts/deploy.sh | bash
#
#   # Deploy only specific services (override the default four):
#   curl -sL https://raw.githubusercontent.com/alivebe-a11y/switching/main/scripts/deploy.sh | bash -s -- dashboard
#
#   # Deploy from a different branch:
#   BRANCH=some-branch curl -sL https://raw.githubusercontent.com/alivebe-a11y/switching/main/scripts/deploy.sh | bash
#
# Or download once and run locally:  ./scripts/deploy.sh
#
set -euo pipefail

REPO="alivebe-a11y/switching"
BRANCH="${BRANCH:-main}"
COMPOSE_FILE="compose.yaml"   # Dockge expects compose.yaml, not docker-compose.yml

# Active services that run the shared image. Override by passing names as args.
# paper-trade / paper-trade-uk / trade-t212 / trade-t212-uk all use paper_trader.py;
# dashboard uses web.py + weekly_report.py.
DEFAULT_SERVICES=(paper-trade paper-trade-uk trade-t212 trade-t212-uk dashboard)
if [ "$#" -gt 0 ]; then
  SERVICES=("$@")
else
  SERVICES=("${DEFAULT_SERVICES[@]}")
fi

# The service whose build we trigger to (re)build the shared image. Building any
# one tags ghcr.io/...:latest for all of them. Use the first requested service.
BUILD_SERVICE="${SERVICES[0]}"

echo "=============================================================="
echo " Switching deploy"
echo "   repo:     ${REPO}"
echo "   branch:   ${BRANCH}"
echo "   services: ${SERVICES[*]}"
echo "=============================================================="

echo
echo "[1/5] Fetching ${COMPOSE_FILE} from ${BRANCH}..."
curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/docker-compose.yml" -o "${COMPOSE_FILE}"
echo "      done."

echo
echo "[2/5] Pruning BuildKit cache (busts the GitHub git-clone cache)..."
docker builder prune -af >/dev/null
echo "      done."

echo
echo "[3/5] Building shared image via '${BUILD_SERVICE}'..."
docker compose build "${BUILD_SERVICE}"

echo
echo "[4/5] Recreating services: ${SERVICES[*]}"
docker compose up -d "${SERVICES[@]}"

echo
echo "[5/5] Verifying — all listed services should share one image ID and show"
echo "      a fresh start time:"
echo
docker compose ps
echo
docker compose images "${SERVICES[@]}" 2>/dev/null || docker compose images

echo
echo "=============================================================="
echo " Deploy complete."
echo " Tail a service to confirm new code is live, e.g.:"
echo "   docker compose logs trade-t212 --tail 20    # expect 'Poll at ...'"
echo "   docker compose logs paper-trade-uk --tail 20"
echo "=============================================================="

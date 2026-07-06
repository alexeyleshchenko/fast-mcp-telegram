#!/usr/bin/env bash
# Deploy fast-mcp-telegram to production.
# Usage: ./scripts/deploy.sh [version]
#   version defaults to `git describe --tags` (or "dev" if not in a git repo)
#
# Prerequisites:
#   - SSH access to the apps host configured in ~/.ssh/config
#   - Docker on the remote host
#   - Source committed (the script copies the working tree via rsync)
#
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-apps}"
REMOTE_DIR="${REMOTE_DIR:-/root/fast-mcp-telegram}"
VERSION="${1:-$(git describe --tags --dirty --always 2>/dev/null || echo "dev")}"
GHCR_IMAGE="ghcr.io/leshchenko1979/fast-mcp-telegram"
COMPOSE_FILE="docker-compose.yml"
SERVICE="fast-mcp-telegram"
CONTAINER_NAME="fast-mcp-telegram"

echo "=== Deploy ${VERSION} → ${REMOTE_HOST} ==="

# ------- 1. Sync source to remote -------
echo "--- Syncing source ---"
rsync -avz --delete --mkpath \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '*.pyo' --exclude '.env' \
    --exclude '.cursor' --exclude 'memory-bank' --exclude 'dist' \
    --exclude '*.egg-info' --exclude '.mypy_cache' \
    --exclude '.pytest_cache' --exclude 'htmlcov' \
    --exclude 'logs' --exclude '*.session' \
    ./ "${REMOTE_HOST}:${REMOTE_DIR}/"

# ------- 2. Save current tag for rollback -------
CURRENT_TAG=""
if ssh "${REMOTE_HOST}" "docker container inspect ${CONTAINER_NAME} >/dev/null 2>&1"; then
    CURRENT_TAG=$(ssh "${REMOTE_HOST}" \
        "docker inspect --format '{{.Config.Image}}' ${CONTAINER_NAME} 2>/dev/null" \
        | awk -F: '{print $NF}')
fi
echo "   Current: ${CURRENT_TAG:-none}  Target: ${VERSION}"

# ------- 3. Build image ----
echo "--- Building ${GHCR_IMAGE}:${VERSION} ---"
ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && docker build -t ${GHCR_IMAGE}:${VERSION} ."

# ------- 4. Deploy via compose ----
echo "--- Deploying via compose ---"
ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && IMAGE_TAG=${VERSION} docker compose up -d ${SERVICE}"

# ------- 5. Wait for healthcheck ----
echo "--- Waiting for healthcheck ---"
HEALTHY=false
for i in $(seq 1 15); do
    STATUS=$(ssh "${REMOTE_HOST}" \
        "docker inspect --format '{{.State.Health.Status}}' ${CONTAINER_NAME} 2>/dev/null || echo 'missing'")
    if [ "${STATUS}" = "healthy" ]; then
        HEALTHY=true
        break
    fi
    echo "  [${i}/15] status=${STATUS}"
    sleep 4
done

# ------- 6. Success or rollback ----
if [ "${HEALTHY}" = "true" ]; then
    echo "✅ ${VERSION} deployed — healthy."
    # Clean up dangling layers only; keep version-tagged images for rollback
    ssh "${REMOTE_HOST}" "docker image prune -f --filter 'dangling=true'"
    exit 0
fi

echo "❌ Healthcheck failed after 60s."
if [ -n "${CURRENT_TAG}" ]; then
    echo "   Rolling back to ${CURRENT_TAG}..."
    ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && IMAGE_TAG=${CURRENT_TAG} docker compose up -d ${SERVICE}"
    # Wait for rollback health
    for i in $(seq 1 8); do
        STATUS=$(ssh "${REMOTE_HOST}" \
            "docker inspect --format '{{.State.Health.Status}}' ${CONTAINER_NAME} 2>/dev/null || echo 'missing'")
        [ "${STATUS}" = "healthy" ] && break
        sleep 4
    done
    echo "   Rolled back to ${CURRENT_TAG}."
else
    echo "   No previous tag — manual intervention required."
fi
exit 1

#!/usr/bin/env bash
# Build every platform image for linux/amd64 and push it to the platform ECR repositories
# (foxtrot/<name>), tagged latest and with the source repository's short commit.
# Usage: AWS_PROFILE=foxtrot ./build-images.sh [name ...]   (default: all)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"    # the directory holding all team repositories
IAC="$ROOT/control-plane/infrastructure"
REGION="${REGION:-ap-northeast-2}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null

build() {  # name, source repo, docker build args...
  local name="$1" repo="$2"; shift 2
  local sha; sha="$(git -C "$repo" rev-parse --short HEAD)"
  local image="${REGISTRY}/foxtrot/${name}"
  echo "== ${name} (${sha})"
  docker buildx build --platform linux/amd64 --provenance=false -t "${image}:${sha}" -t "${image}:latest" --push "$@"
}

want() { [ -z "${TARGETS}" ] || [[ " ${TARGETS} " == *" $1 "* ]]; }
TARGETS="${*:-}"
want control-plane && GITHUB_TOKEN="${GITHUB_TOKEN:-$(gh auth token --user yyoungjin)}" build control-plane "$ROOT/control-plane" \
  --secret id=github_token,env=GITHUB_TOKEN --build-arg DB_OPERATOR_REF="${DB_OPERATOR_REF:-68a0c10}" "$ROOT/control-plane"
want db-migration-adaptor && build db-migration-adaptor "$ROOT/db-migration-adaptor" "$ROOT/db-migration-adaptor"
want redis-proxy && build redis-proxy "$ROOT/redis-migrator" "$ROOT/redis-migrator/proxy"
want redis-statemig && build redis-statemig "$ROOT/redis-migrator" "$ROOT/redis-migrator"
want object-migration && build object-migration "$ROOT/object-migration" "$ROOT/object-migration"
want traffic && build traffic "$ROOT/traffic" "$ROOT/traffic/server"
want platform-console && build platform-console "$ROOT/platform-frontend" -f "$IAC/environments/platform/console/Dockerfile" \
  --build-context console="$IAC/environments/platform/console" "$ROOT/platform-frontend"
echo "all images pushed to ${REGISTRY}/foxtrot/*"

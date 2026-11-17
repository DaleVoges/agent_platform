#!/usr/bin/env bash
# Spin up / manage extra Hermes instances locally, one container per agent.
# This is a stand-in for what your control plane will do later
# (Docker API locally, Fly Machines API in production).
#
#   scripts/instance.sh create acme      # new instance, prints port + API key
#   scripts/instance.sh list
#   scripts/instance.sh logs acme
#   scripts/instance.sh stop acme | start acme
#   scripts/instance.sh rm acme          # removes container, keeps data dir
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
IMAGE="${HERMES_IMAGE:-nousresearch/hermes-agent:latest}"
cmd="${1:-}"; name="${2:-}"

need_name() { [[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "name must be [a-z0-9-]+"; exit 1; }; }

next_port() {
  local p=8650
  while docker ps -a --format '{{.Ports}}' | grep -q ":$p->"; do p=$((p+1)); done
  echo $p
}

case "$cmd" in
  create)
    need_name
    port=$(next_port)
    key=$(openssl rand -hex 32)
    mkdir -p "data/instances/$name"
    docker run -d --name "hermes-$name" --restart unless-stopped \
      --label platform.instance="$name" \
      -v "$PWD/data/instances/$name:/opt/data" \
      -p "127.0.0.1:$port:8642" \
      -e HERMES_UID="$(id -u)" -e HERMES_GID="$(id -g)" \
      -e API_SERVER_ENABLED=true -e API_SERVER_HOST=0.0.0.0 \
      -e API_SERVER_KEY="$key" \
      ${OPENROUTER_API_KEY:+-e OPENROUTER_API_KEY="$OPENROUTER_API_KEY"} \
      ${ANTHROPIC_API_KEY:+-e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"} \
      ${OPENAI_API_KEY:+-e OPENAI_API_KEY="$OPENAI_API_KEY"} \
      ${LLM_MODEL:+-e LLM_MODEL="$LLM_MODEL"} \
      "$IMAGE" gateway run >/dev/null
    echo "instance=$name url=http://127.0.0.1:$port/v1 api_key=$key"
    echo "$key" > "data/instances/$name/.platform_api_key"; chmod 600 "data/instances/$name/.platform_api_key"
    ;;
  list)  docker ps -a --filter label=platform.instance --format 'table {{.Label "platform.instance"}}\t{{.Status}}\t{{.Ports}}' ;;
  logs)  need_name; docker logs -f "hermes-$name" ;;
  stop|start) need_name; docker "$cmd" "hermes-$name" ;;
  rm)    need_name; docker rm -f "hermes-$name" ;;
  *) sed -n 2,12p "$0"; exit 1 ;;
esac

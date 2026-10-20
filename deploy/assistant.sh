#!/usr/bin/env bash
# Turn the AI phone assistant on or off on the server.
#
#   sudo bash deploy/assistant.sh setup   # choose the brain (Claude or OpenAI), enter its API key
#   sudo bash deploy/assistant.sh test    # check the model connection (tool calls too)
#   sudo bash deploy/assistant.sh off     # back to the keypad-only call
#   sudo bash deploy/assistant.sh status
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ENV=.env
[[ -f "$ENV" ]] || { echo "deploy/.env not found: run install.sh first" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 2; }

set_var() {
  local tmp; tmp="$(mktemp)"
  grep -v -E "^$1=" "$ENV" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > "$ENV"; rm -f "$tmp"; chmod 600 "$ENV"
}
get_var() { grep -E "^$1=" "$ENV" | tail -1 | cut -d= -f2- || true; }
restart() {
  echo "Rebuilding and restarting (the first build downloads ~1 GB, the first start ~1.6 GB)..."
  docker compose --env-file "$ENV" up -d --build
}

case "${1:-}" in
  setup)
    MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
    echo "Server memory: ${MEM_MB} MB (Whisper large-v3-turbo needs about 2 GB free)."
    echo "How to reach the model:"
    echo "  1) An API gateway with an OpenAI-style API (e.g. https://api-models.com/v1)"
    echo "  2) An API gateway with an Anthropic-style API"
    echo "  3) Anthropic directly (console.anthropic.com)   4) OpenAI directly"
    read -r -p "Choose 1-4 [1]: " B
    case "${B:-1}" in
      1|4) set_var AVB_BRAIN openai ;;
      *)   set_var AVB_BRAIN anthropic ;;
    esac
    URL=""
    if [[ "${B:-1}" == 1 || "${B:-1}" == 2 ]]; then
      read -r -p "Gateway base URL (from its docs, e.g. https://api-models.com/v1): " URL
      [[ -n "$URL" ]] || { echo "a base URL is needed" >&2; exit 2; }
    fi
    set_var AVB_BRAIN_BASE_URL "$URL"
    read -r -p "Model [claude-haiku-4-5]: " M
    set_var AVB_BRAIN_MODEL "${M:-claude-haiku-4-5}"
    read -r -s -p "API key: " K; echo
    [[ -n "$K" ]] || { echo "an API key is needed" >&2; exit 2; }
    set_var AVB_BRAIN_API_KEY "$K"
    DEFAULT_STT=large-v3-turbo; (( MEM_MB < 3000 )) && DEFAULT_STT=small
    read -r -p "Whisper model [${DEFAULT_STT}] (large-v3-turbo, small, base): " S
    set_var AVB_STT_MODEL "${S:-$DEFAULT_STT}"
    read -r -p "Language you'll speak, e.g. en or fa (empty = detect): " L
    set_var AVB_STT_LANGUAGE "${L:-}"
    set_var AVB_ASSISTANT 1
    restart
    echo "Checking the model connection (incl. tool calls)..."
    sleep 5
    docker compose --env-file "$ENV" exec bridge avb-admin brain-test || \
      echo "The model check failed: see above. Fix with: sudo bash deploy/assistant.sh setup"
    echo "Done. Watch it load: sudo docker compose logs -f bridge"
    ;;
  off)
    set_var AVB_ASSISTANT 0; restart ;;
  test)
    docker compose --env-file "$ENV" exec bridge avb-admin brain-test ;;
  status)
    echo "Assistant: $(get_var AVB_ASSISTANT) brain=$(get_var AVB_BRAIN) model=$(get_var AVB_BRAIN_MODEL) stt=$(get_var AVB_STT_MODEL)" ;;
  *)
    sed -n '2,7p' "$0"; exit 2 ;;
esac

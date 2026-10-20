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
    echo "  1) API Models gateway (api-models.com)  - URL filled in for you"
    echo "  2) Another gateway with an OpenAI-style API"
    echo "  3) Another gateway with an Anthropic-style API"
    echo "  4) Anthropic directly (console.anthropic.com)   5) OpenAI directly"
    read -r -p "Choose 1-5 [1]: " B
    B="${B:-1}"
    read -r -p "Model [claude-haiku-4-5] (e.g. gpt-5.6-sol): " M
    M="${M:-claude-haiku-4-5}"
    URL=""
    case "$B" in
      1) # Claude models over the gateway's Anthropic API, others over its OpenAI API.
         if [[ "$M" == claude-* ]]; then PROTO=anthropic; URL=https://api-models.com
         else PROTO=openai; URL=https://api-models.com/v1; fi ;;
      2) PROTO=openai; read -r -p "Gateway base URL (e.g. https://example.com/v1): " URL ;;
      3) PROTO=anthropic; read -r -p "Gateway base URL (e.g. https://example.com): " URL ;;
      4) PROTO=anthropic ;;
      *) PROTO=openai ;;
    esac
    if [[ "$B" == 2 || "$B" == 3 ]] && [[ -z "$URL" ]]; then echo "a base URL is needed" >&2; exit 2; fi
    set_var AVB_BRAIN "$PROTO"
    set_var AVB_BRAIN_BASE_URL "$URL"
    set_var AVB_BRAIN_MODEL "$M"
    read -r -s -p "API key: " K; echo
    [[ -n "$K" ]] || { echo "an API key is needed" >&2; exit 2; }
    set_var AVB_BRAIN_API_KEY "$K"
    echo "Speech-to-text: 1) online service (fast, accurate; recommended)   2) Whisper on this server"
    read -r -p "Choose 1 or 2 [1]: " T
    if [[ "${T:-1}" == 2 ]]; then
      DEFAULT_STT=large-v3-turbo; (( MEM_MB < 3000 )) && DEFAULT_STT=small
      read -r -p "Whisper model [${DEFAULT_STT}] (large-v3-turbo, small, base): " S
      set_var AVB_STT local; set_var AVB_STT_MODEL "${S:-$DEFAULT_STT}"
      set_var AVB_STT_BASE_URL ""; set_var AVB_STT_API_KEY ""
    else
      read -r -p "Speech-to-text model [gpt-4o-mini-transcribe] (check the gateway's catalog): " S
      set_var AVB_STT api; set_var AVB_STT_MODEL "${S:-gpt-4o-mini-transcribe}"
      SURL="https://api-models.com/v1"; [[ "$B" == 2 || "$B" == 5 ]] && SURL="${URL:-https://api.openai.com/v1}"
      read -r -p "Speech-to-text API URL [$SURL]: " SU
      set_var AVB_STT_BASE_URL "${SU:-$SURL}"
      read -r -s -p "Its API key [Enter = same key as above]: " SK; echo
      set_var AVB_STT_API_KEY "${SK:-$K}"
    fi
    read -r -p "Language you'll speak, e.g. en or fa (empty = detect): " L
    set_var AVB_STT_LANGUAGE "${L:-}"
    set_var AVB_ASSISTANT 1
    restart
    echo "Checking the model connection (incl. tool calls)..."
    sleep 5
    docker compose --env-file "$ENV" exec bridge avb-admin brain-test || \
      echo "The model check failed: see above. Fix with: sudo bash deploy/assistant.sh setup"
    echo "Checking speech-to-text..."
    docker compose --env-file "$ENV" exec bridge avb-admin stt-test || \
      echo "The speech-to-text check failed: see above."
    echo "Done. Watch it load: sudo docker compose logs -f bridge"
    ;;
  off)
    set_var AVB_ASSISTANT 0; restart ;;
  test)
    docker compose --env-file "$ENV" exec bridge avb-admin brain-test
    docker compose --env-file "$ENV" exec bridge avb-admin stt-test ;;
  status)
    echo "Assistant: $(get_var AVB_ASSISTANT) brain=$(get_var AVB_BRAIN) model=$(get_var AVB_BRAIN_MODEL) stt=$(get_var AVB_STT_MODEL)" ;;
  *)
    sed -n '2,7p' "$0"; exit 2 ;;
esac

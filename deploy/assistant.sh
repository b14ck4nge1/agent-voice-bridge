#!/usr/bin/env bash
# Turn the AI phone assistant on or off on the server.
#
#   sudo bash deploy/assistant.sh setup   # choose the brain (Claude or OpenAI), enter its API key
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
    echo "Brain: 1) Claude (Anthropic API)   2) OpenAI"
    read -r -p "Choose 1 or 2 [1]: " B
    if [[ "${B:-1}" == 2 ]]; then
      read -r -p "OpenAI model [gpt-5.6-sol]: " M
      read -r -s -p "OpenAI API key: " K; echo
      set_var AVB_BRAIN openai; set_var AVB_BRAIN_MODEL "${M:-gpt-5.6-sol}"; set_var OPENAI_API_KEY "$K"
    else
      read -r -p "Claude model [claude-haiku-4-5]: " M
      read -r -s -p "Anthropic API key (console.anthropic.com): " K; echo
      set_var AVB_BRAIN anthropic; set_var AVB_BRAIN_MODEL "${M:-claude-haiku-4-5}"; set_var ANTHROPIC_API_KEY "$K"
    fi
    [[ -n "$K" ]] || { echo "an API key is needed" >&2; exit 2; }
    DEFAULT_STT=large-v3-turbo; (( MEM_MB < 3000 )) && DEFAULT_STT=small
    read -r -p "Whisper model [${DEFAULT_STT}] (large-v3-turbo, small, base): " S
    set_var AVB_STT_MODEL "${S:-$DEFAULT_STT}"
    read -r -p "Language you'll speak, e.g. en or fa (empty = detect): " L
    set_var AVB_STT_LANGUAGE "${L:-}"
    set_var AVB_ASSISTANT 1
    restart
    echo "Done. Watch it load: sudo docker compose logs -f bridge"
    ;;
  off)
    set_var AVB_ASSISTANT 0; restart ;;
  status)
    echo "Assistant: $(get_var AVB_ASSISTANT) brain=$(get_var AVB_BRAIN) model=$(get_var AVB_BRAIN_MODEL) stt=$(get_var AVB_STT_MODEL)" ;;
  *)
    sed -n '2,6p' "$0"; exit 2 ;;
esac

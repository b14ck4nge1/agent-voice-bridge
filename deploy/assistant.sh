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
# ask VAR "question" default: shows the saved value (or the default); Enter keeps it.
ask_value() {
  local answer
  read -r -p "$1 [$2]: " answer </dev/tty
  echo "${answer:-$2}"
}
ask() {
  local cur; cur="$(get_var "$1")"
  ask_value "$2" "${cur:-$3}"
}
# ask_secret VAR "question" [fallback]: shows only the end of the saved key.
ask_secret() {
  local cur answer hint
  cur="$(get_var "$1")"; cur="${cur:-${3:-}}"
  hint="none"; [[ -n "$cur" ]] && hint="saved, ends in ...${cur: -4}"
  read -r -s -p "$2 [$hint, Enter keeps it]: " answer </dev/tty; echo >&2
  echo "${answer:-$cur}"
}
restart() {
  echo "Rebuilding and restarting (the first build downloads ~1 GB, the first start ~1.6 GB)..."
  docker compose --env-file "$ENV" up -d --build
}

case "${1:-}" in
  setup)
    if [[ "$(get_var AVB_ASSISTANT)" == 1 ]]; then
      echo "Current setup: brain $(get_var AVB_BRAIN_MODEL) via $(get_var AVB_BRAIN_BASE_URL || true)," \
        "speech-to-text $(get_var AVB_STT) $(get_var AVB_STT_MODEL)"
    fi
    echo "Press Enter to keep the value in [brackets]; type something to change it."
    echo
    MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
    echo "How to reach the model:"
    echo "  1) API Models gateway (api-models.com)  - URL filled in for you"
    echo "  2) Another gateway with an OpenAI-style API"
    echo "  3) Another gateway with an Anthropic-style API"
    echo "  4) Anthropic directly (console.anthropic.com)   5) OpenAI directly"
    B=$(ask AVB_BRAIN_ROUTE "Choose 1-5" 1)
    M=$(ask AVB_BRAIN_MODEL "Model (e.g. gemini-2.5-flash, claude-haiku-4-5)" gemini-2.5-flash)
    case "$B" in
      1) # Claude models over the gateway's Anthropic API, others over its OpenAI API.
         if [[ "$M" == claude-* ]]; then PROTO=anthropic; URL=https://api-models.com
         else PROTO=openai; URL=https://api-models.com/v1; fi ;;
      2) PROTO=openai; URL=$(ask AVB_BRAIN_BASE_URL "Gateway base URL (e.g. https://example.com/v1)" "") ;;
      3) PROTO=anthropic; URL=$(ask AVB_BRAIN_BASE_URL "Gateway base URL (e.g. https://example.com)" "") ;;
      4) PROTO=anthropic; URL="" ;;
      *) PROTO=openai; URL="" ;;
    esac
    if [[ "$B" == 2 || "$B" == 3 ]] && [[ -z "$URL" ]]; then echo "a base URL is needed" >&2; exit 2; fi
    K=$(ask_secret AVB_BRAIN_API_KEY "API key")
    [[ -n "$K" ]] || { echo "an API key is needed" >&2; exit 2; }
    set_var AVB_BRAIN_ROUTE "$B"; set_var AVB_BRAIN "$PROTO"; set_var AVB_BRAIN_BASE_URL "$URL"
    set_var AVB_BRAIN_MODEL "$M"; set_var AVB_BRAIN_API_KEY "$K"

    echo
    echo "Speech-to-text:"
    echo "  1) Gemini listens to the audio (gemini-2.5-flash via API Models; recommended)"
    echo "  2) An OpenAI-style transcription API (whisper-1, gpt-4o-mini-transcribe, ...)"
    echo "  3) Whisper on this server (slow on a small CPU)"
    case "$(get_var AVB_STT)" in api) CUR=2 ;; local) CUR=3 ;; *) CUR=1 ;; esac
    read -r -p "Choose 1-3 [$CUR]: " T; T="${T:-$CUR}"
    SAME_STT=0; [[ "$T" == "$CUR" && -n "$(get_var AVB_STT)" ]] && SAME_STT=1
    stt_default() { if [[ $SAME_STT == 1 ]]; then get_var "$1"; else echo "$2"; fi; }
    if [[ "$T" == 1 ]]; then
      S=$(ask_value "Gemini model" "$(stt_default AVB_STT_MODEL gemini-2.5-flash)")
      SU=$(ask_value "Gemini API host" "$(stt_default AVB_STT_BASE_URL https://api-models.com)")
      set_var AVB_STT gemini; set_var AVB_STT_MODEL "$S"; set_var AVB_STT_BASE_URL "$SU"
      set_var AVB_STT_API_KEY "$(ask_secret AVB_STT_API_KEY "Its API key" "$K")"
    elif [[ "$T" == 3 ]]; then
      DEFAULT_STT=large-v3-turbo; (( MEM_MB < 3000 )) && DEFAULT_STT=small
      S=$(ask_value "Whisper model (large-v3-turbo, small, base)" "$(stt_default AVB_STT_MODEL $DEFAULT_STT)")
      set_var AVB_STT local; set_var AVB_STT_MODEL "$S"
      set_var AVB_STT_BASE_URL ""; set_var AVB_STT_API_KEY ""
    else
      SURL="https://api-models.com/v1"; [[ "$B" == 2 || "$B" == 5 ]] && SURL="${URL:-https://api.openai.com/v1}"
      S=$(ask_value "Speech-to-text model" "$(stt_default AVB_STT_MODEL gpt-4o-mini-transcribe)")
      SU=$(ask_value "Speech-to-text API URL" "$(stt_default AVB_STT_BASE_URL $SURL)")
      set_var AVB_STT api; set_var AVB_STT_MODEL "$S"; set_var AVB_STT_BASE_URL "$SU"
      set_var AVB_STT_API_KEY "$(ask_secret AVB_STT_API_KEY "Its API key" "$K")"
    fi
    L=$(ask AVB_STT_LANGUAGE "Language you'll speak, e.g. en or fa (- = detect)" "")
    [[ "$L" == "-" ]] && L=""
    set_var AVB_STT_LANGUAGE "$L"
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

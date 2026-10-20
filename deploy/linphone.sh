#!/usr/bin/env bash
# Choose how the bridge reaches your iPhone, and test the Linphone-account path.
#
#   sudo bash deploy/linphone.sh setup   # enter the two Linphone accounts (saved in deploy/.env)
#   sudo bash deploy/linphone.sh test    # one test call to your Linphone account (keypad echo)
#   sudo bash deploy/linphone.sh use     # approvals call you via Linphone (no Bark)
#   sudo bash deploy/linphone.sh bark    # back to Bark + your own SIP server
#   sudo bash deploy/linphone.sh status  # which one is active
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ENV=.env
[[ -f "$ENV" ]] || { echo "deploy/.env not found: run install.sh first" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 2; }

set_var() {  # set_var NAME VALUE: replace or append, keeping the file private
  local tmp; tmp="$(mktemp)"
  grep -v -E "^$1=" "$ENV" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > "$ENV"; rm -f "$tmp"; chmod 600 "$ENV"
}
get_var() { grep -E "^$1=" "$ENV" | tail -1 | cut -d= -f2- || true; }
restart() { docker compose --env-file "$ENV" up -d --build bridge; }

case "${1:-}" in
  setup)
    echo "The bridge needs its OWN Linphone account to call yours from (a second free"
    echo "account from https://subscribe.linphone.org). Your iPhone stays signed in to yours."
    read -r -p "Bridge's Linphone username: " BOT
    read -r -s -p "Bridge's Linphone password: " PW; echo
    read -r -p "YOUR Linphone username (the one on the iPhone): " ME
    BOT="${BOT#sip:}"; BOT="${BOT%@sip.linphone.org}"; ME="${ME#sip:}"; ME="${ME%@sip.linphone.org}"
    [[ -n "$BOT" && -n "$PW" && -n "$ME" ]] || { echo "all three are needed" >&2; exit 2; }
    [[ "$BOT" != "$ME" ]] || { echo "the two accounts must be different" >&2; exit 2; }
    set_var AVB_LINPHONE_USERNAME "$BOT"
    set_var AVB_LINPHONE_PASSWORD "$PW"
    set_var AVB_LINPHONE_CALL "$ME"
    restart
    echo "Saved. Next: sudo bash deploy/linphone.sh test"
    ;;
  test)
    [[ -n "$(get_var AVB_LINPHONE_USERNAME)" ]] || { echo "run: sudo bash deploy/linphone.sh setup" >&2; exit 2; }
    echo "Calling your Linphone account. Answer, listen, press keys, press # to finish."
    docker compose --env-file "$ENV" exec bridge avb-admin phone-test --linphone
    ;;
  use)
    [[ -n "$(get_var AVB_LINPHONE_USERNAME)" ]] || { echo "run: sudo bash deploy/linphone.sh setup" >&2; exit 2; }
    set_var AVB_CALL_DISPATCHER linphone
    restart
    echo "Approvals now call your Linphone account. Logs: sudo docker compose logs -f bridge"
    ;;
  bark)
    set_var AVB_CALL_DISPATCHER bark-sip
    restart
    echo "Approvals now use Bark + your own SIP server."
    ;;
  status)
    MODE="$(get_var AVB_CALL_DISPATCHER)"; echo "Mode: ${MODE:-bark-sip (default)}"
    echo "Linphone: bridge=$(get_var AVB_LINPHONE_USERNAME) calls=$(get_var AVB_LINPHONE_CALL)"
    ;;
  *)
    sed -n '2,8p' "$0"; exit 2 ;;
esac

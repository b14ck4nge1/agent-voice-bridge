#!/usr/bin/env bash
# Install Agent Voice Bridge on an Ubuntu server (tested target: Ubuntu 24.04+/26.04).
#
#   git clone --branch claude/phase0-bridge-mvp https://github.com/b14ck4nge1/agent-voice-bridge.git
#   sudo bash agent-voice-bridge/deploy/install.sh voice.example.com YOUR_BARK_DEVICE_KEY
# (private repo: clone with a read-only GitHub token, see docs/server-setup.md)
#
# Installs Docker, opens the firewall (SSH, 80/443, SIP-TLS 5061, RTP 40000-40200/udp),
# generates secrets, starts Caddy (HTTPS) + the bridge, and writes SETUP-DETAILS.txt
# with what to enter on the iPhone and on the PC. Re-running keeps existing secrets.
set -euo pipefail

DOMAIN="${1:-}"
BARK_KEY="${2:-}"
BRANCH="${AVB_BRANCH:-claude/phase0-bridge-mvp}"
# Run from a checkout (e.g. a private repo you cloned yourself) to use it in place.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/../pyproject.toml" ]]; then
  DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  DIR=/opt/agent-voice-bridge
fi

if [[ -z "$DOMAIN" || -z "$BARK_KEY" ]]; then
  echo "usage: sudo bash install.sh <domain, e.g. voice.example.com> <Bark device key>" >&2
  exit 2
fi
if [[ $EUID -ne 0 ]]; then echo "run with sudo" >&2; exit 2; fi

echo "== Installing packages"
apt-get update -qq
apt-get install -y -qq git curl ufw python3 ca-certificates >/dev/null
if ! command -v docker >/dev/null; then
  apt-get install -y -qq docker.io >/dev/null
fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 || apt-get install -y -qq docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null

echo "== Checking DNS"
PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)"
DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
echo "   server public IP: ${PUBLIC_IP:-unknown}   $DOMAIN resolves to: ${DNS_IP:-nothing}"
if [[ -z "$DNS_IP" || "$DNS_IP" != "$PUBLIC_IP" ]]; then
  echo "   WARNING: $DOMAIN does not point at this server yet."
  echo "   In Cloudflare: DNS > A record '$DOMAIN' -> $PUBLIC_IP, Proxy status = DNS only (grey cloud)."
  echo "   The certificate (and calls) will only work once it does. Continuing anyway."
fi

echo "== Firewall"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 5061/tcp >/dev/null
ufw allow 40000:40200/udp >/dev/null
ufw --force enable >/dev/null

echo "== Fetching code"
if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" pull -q --ff-only || echo "   (could not update; using the code already in $DIR)"
else
  git clone -q --branch "$BRANCH" https://github.com/b14ck4nge1/agent-voice-bridge.git "$DIR"
fi
cd "$DIR/deploy"

ENV=.env
rand() { python3 -c "import secrets,string;a=string.ascii_letters+string.digits;print(''.join(secrets.choice(a) for _ in range($1)))"; }
get() { grep -E "^$1=" "$ENV" 2>/dev/null | head -1 | cut -d= -f2- || true; }
AGENT_TOKEN="$(get AVB_AGENT_TOKEN)"; [[ -n "$AGENT_TOKEN" ]] || AGENT_TOKEN="$(rand 43)"
VOICE_TOKEN="$(get AVB_VOICE_TOKEN)"; [[ -n "$VOICE_TOKEN" ]] || VOICE_TOKEN="$(rand 43)"
SIP_PASSWORD="$(get AVB_SIP_PASSWORD)"; [[ -n "$SIP_PASSWORD" ]] || SIP_PASSWORD="$(rand 24)"
BARK_CRYPTO="$(get AVB_BARK_ENCRYPTION_KEY)"; [[ -n "$BARK_CRYPTO" ]] || BARK_CRYPTO="$(rand 32)"
TOTP="$(get AVB_TOTP_SECRET)"
[[ -n "$TOTP" ]] || TOTP="$(python3 -c "import base64,secrets;print(base64.b32encode(secrets.token_bytes(20)).decode().rstrip('='))")"
BARK_IV="$(rand 12)"

umask 077
cat > "$ENV" <<EOF
# Agent Voice Bridge server settings (secrets). Keep private.
AVB_DOMAIN=$DOMAIN
AVB_AGENT_TOKEN=$AGENT_TOKEN
AVB_VOICE_TOKEN=$VOICE_TOKEN
AVB_TOTP_SECRET=$TOTP
AVB_BARK_DEVICE_KEY=$BARK_KEY
AVB_BARK_ENCRYPTION_KEY=$BARK_CRYPTO
AVB_SIP_BIND=0.0.0.0
AVB_SIP_PUBLIC_HOST=$DOMAIN
AVB_SIP_MEDIA_IP=${PUBLIC_IP}
AVB_SIP_PASSWORD=$SIP_PASSWORD
AVB_SIP_TRANSPORT=tls
EOF

echo "== Starting (first build takes a few minutes)"
docker compose --env-file "$ENV" up -d --build

echo "== Waiting for HTTPS"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "https://$DOMAIN/healthz" >/dev/null 2>&1; then OK=1; break; fi
  sleep 5
done
if [[ "${OK:-0}" == 1 ]]; then echo "   https://$DOMAIN is up"; else
  echo "   HTTPS not up yet. Check DNS, then: docker compose -f $DIR/deploy/docker-compose.yml logs caddy"; fi

DETAILS="$DIR/deploy/SETUP-DETAILS.txt"
cat > "$DETAILS" <<EOF
AGENT VOICE BRIDGE - SETUP DETAILS  (secrets: keep private, delete after setup)

=== iPhone: Bark ===
Allow notifications and Critical Alerts.
Settings > Encryption:  Algorithm AES256,  Mode GCM,  Padding noPadding
  Key: $BARK_CRYPTO
  IV:  $BARK_IV   (any 12 characters; each notification carries its own)

=== iPhone: Linphone ===
"Use SIP account" (third-party SIP account, NOT a linphone.org account):
  Username:  phone
  Password:  $SIP_PASSWORD
  Domain:    $DOMAIN:5061
  Transport: TLS
Account / call settings: Media encryption = SRTP. Push notifications: off.

=== iPhone: authenticator app (for high-risk approvals) ===
  otpauth://totp/AgentVoiceBridge:approvals?secret=$TOTP&issuer=AgentVoiceBridge&digits=6&period=30

=== Windows PC ===
In PowerShell, in the folder you want:
  git clone --branch $BRANCH https://github.com/b14ck4nge1/agent-voice-bridge.git
  powershell -ExecutionPolicy Bypass -File .\\agent-voice-bridge\\scripts\\setup-pc-remote.ps1 -Url https://$DOMAIN -Token $AGENT_TOKEN
EOF
chmod 600 "$DETAILS"
echo
cat "$DETAILS"
echo
echo "Saved to $DETAILS"

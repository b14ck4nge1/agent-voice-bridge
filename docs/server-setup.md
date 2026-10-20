# Server setup (Bark + Linphone only, works anywhere)

Your Ubuntu server runs the bridge. The iPhone needs only **Bark** and **Linphone**;
the PC's hooks talk to the server over HTTPS. Calls use SIP over TLS (Let's Encrypt
certificate) and SRTP-encrypted audio. See [ADR-0006](adr/0006-public-server.md).

## 1. Point your domain at the server (Cloudflare, free)

1. Create a free Cloudflare account and **Add a domain**. Cloudflare shows two
   nameservers; set them at the company where you bought the domain (usually under
   "Nameservers" or "DNS"). It takes from a few minutes to a few hours.
2. In Cloudflare: **DNS → Records → Add record**:
   * Type **A**, Name **voice**, IPv4 = your server's public IP
   * Proxy status: **DNS only** (grey cloud). Calls can't go through Cloudflare's proxy.
3. Check from any PC: `nslookup voice.yourdomain.com` should print the server IP.

If your provider has a firewall in its web panel, also allow: TCP 22, 80, 443,
5061 and UDP 40000-40200.

## 2. Install on the server (Ubuntu 24.04 / 26.04)

```bash
curl -fsSLO https://raw.githubusercontent.com/b14ck4nge1/agent-voice-bridge/claude/phase0-bridge-mvp/deploy/install.sh
sudo bash install.sh voice.yourdomain.com YOUR_BARK_DEVICE_KEY
```

The Bark device key is in the Bark app: the part after `https://api.day.app/` in its
example URL. The script installs Docker, opens the firewall (keeping SSH open),
generates every secret, starts the service, and prints **SETUP-DETAILS.txt**
(`/opt/agent-voice-bridge/deploy/SETUP-DETAILS.txt`) with what to enter on the
iPhone and the PC.

## 3. iPhone

Follow SETUP-DETAILS.txt:
* **Bark**: encryption settings (AES256 / GCM / key / IV).
* **Linphone**: third-party SIP account `phone`, domain `voice.yourdomain.com:5061`,
  transport **TLS**, media encryption **SRTP**.
* Optional: the `otpauth://` line in an authenticator app, for high-risk approvals.

## 4. Windows PC

Run the two PowerShell lines printed in SETUP-DETAILS.txt (clone, then
`setup-pc-remote.ps1 -Url https://voice.yourdomain.com -Token ...`). Restart Claude
Code; approve the new hook once in Codex.

## 5. Test

On the PC: `.\.venv\Scripts\avb-admin test-approval`. Bark rings the iPhone; tap it;
Linphone calls the server; you hear the request; press **1**, then **1** to approve.
The PC prints `Decision: allow`.

## Maintenance

```bash
cd /opt/agent-voice-bridge/deploy
sudo docker compose logs -f bridge      # watch calls
sudo bash ../deploy/install.sh voice.yourdomain.com KEY   # update (keeps secrets)
```

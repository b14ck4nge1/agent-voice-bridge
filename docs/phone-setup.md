# Phone setup (Bark + Tailscale + Linphone)

Architecture: [ADR-0005](adr/0005-bark-wake-and-own-sip-server.md).

## On the iPhone (all free, App Store)

1. **Tailscale**: sign in with the same account you use on the PC.
2. **Bark**: allow notifications and Critical Alerts. Note the device key shown
   in the app.
3. **Linphone**: set up a third-party SIP account later with the values from step 2
   below.

## On the PC

1. Install **Tailscale** for Windows and sign in.
2. In PowerShell, in the folder where you want the project:

   ```powershell
   git clone --branch claude/phase0-bridge-mvp https://github.com/b14ck4nge1/agent-voice-bridge.git
   powershell -ExecutionPolicy Bypass -File .\agent-voice-bridge\scripts\setup-phone.ps1
   ```

   It installs everything, asks for the Bark device key, finds the Tailscale IP, and
   writes **PHONE-SETUP.txt** with the exact Linphone and Bark values. Run it as
   Administrator to add the firewall rules automatically; otherwise it prints them.
3. Test: `.\.venv\Scripts\avb-admin phone-test`, then tap the notification on the
   iPhone. You should hear a voice, and keys you press are read back.
4. Run for real: `scripts\start.ps1`, then `avb-admin install-hooks --claude --codex`.

## During a call

You hear which agent and project is asking and what it wants to do. Keys:
**1** approve, **2** reject, **3** details, **4** risks, **5** project, **0** repeat,
**9** skip. Confirm with **1** (yes) or **2** (no). High-risk approvals also need
the 6-digit code from your authenticator app.

## Settings (bridge.env)

See `src/agent_voice_bridge/phone_config.py` for every variable. Most useful:
`AVB_BARK_LEVEL=timeSensitive` (if you don't want critical alerts) and
`AVB_SIP_TRANSPORT=tcp` (if UDP is unreliable on your network).

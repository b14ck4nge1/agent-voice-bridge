# AI phone assistant

With the assistant on, the call is a conversation instead of a keypad menu:

* **It calls you** when an agent needs permission, and when a task you started by phone
  finishes (Linphone mode: your phone rings; Bark mode: a notification you tap).
* **You talk, it talks back.** Your speech goes through Whisper (large-v3-turbo, on your
  server), an LLM brain answers (Claude Haiku 4.5 by default, or an OpenAI model), and
  Piper speaks the answer. Keys still work: 1 = yes, 2 = no.
* **You call it** and ask for things: "search how the Stripe refunds API works", "what's
  the status of my last chat?", "tell that task to also check disputes". It starts a new
  Claude Code session on your PC, reads you reports, and sends follow-ups.

## Safety

* The brain can't approve anything. When you say "approve", it asks the system, and the
  system itself reads the request's summary back and waits for **your** yes (key 1 or
  "yes"). High-risk requests also need your authenticator code on the keypad.
  Text from agents (commands, reasons, task output) is treated as data.
* Starting or continuing a task also needs your yes. The task itself runs with Claude
  Code's normal permissions, so anything that needs approval rings you (or, if you're
  already on the call, comes up in the conversation).
* Calls **to** the bridge's Linphone account are answered only when they come from your
  own Linphone account; others are refused. This relies on sip.linphone.org showing the
  real caller. For extra safety set `AVB_INBOUND_CODE=1` in `deploy/.env` to also ask for
  your authenticator code. In Bark mode your phone signs in with the SIP password.

## Setup

**Server** (once): get an API key, either from an API gateway or directly from
<https://console.anthropic.com> or OpenAI. For **API Models** (api-models.com) choose
option 1 in the setup: Claude models (`claude-haiku-4-5`) go through its Anthropic API
(`https://api-models.com`), others (`gpt-5.6-sol`) through its OpenAI API
(`https://api-models.com/v1`). Its prices are in CNY; give the key a low quota to start. Note that a gateway sees
everything the brain sees: what you say on the call, the requests your agents make,
and task reports. Then

```bash
cd ~/agent-voice-bridge && git pull
sudo bash deploy/assistant.sh setup     # gateway URL, model, API key, Whisper model, language
sudo bash deploy/assistant.sh test      # checks the model answers and makes tool calls
sudo docker compose -f deploy/docker-compose.yml logs -f bridge   # wait for the model to load
```

Speech-to-text: **Gemini** (option 1, `gemini-2.5-flash` through API Models' Gemini API)
listens to each sentence and writes it down, without "thinking", so it's quick. Option 2
is any OpenAI-style transcription API. Otherwise: choose the **online service** unless you want everything on
your server. It sends each sentence you say to `/v1/audio/transcriptions` (API Models,
OpenAI, or another compatible service; default model `gpt-4o-mini-transcribe`), which is
much faster and more accurate than Whisper on a small CPU. `sudo bash deploy/assistant.sh
test` checks it with a spoken test sentence. With local Whisper, the first start downloads
the model (about 1.6 GB). large-v3-turbo needs about 2 GB of free memory; on a smaller server
choose `small`.

The Caddyfile now also exposes `/v1/worker/*` (agent token required) for the PC helper,
so restart Caddy once: `sudo docker compose -f deploy/docker-compose.yml restart caddy`.

**PC**: update and re-run the setup script; it now also starts the PC helper
(`avb-worker`) and adds it to your Startup folder:

```powershell
cd agent-voice-bridge; git pull; cd ..
powershell -ExecutionPolicy Bypass -File .\agent-voice-bridge\scripts\setup-pc-remote.ps1 -Url https://voice.n8nweb.ir -Token <AVB_AGENT_TOKEN>
```

Phone tasks run in `%USERPROFILE%\AgentVoiceBridge-Tasks`. They are Claude Code sessions
started with `claude -p`; open one later with `claude --resume <session id>` (they don't
appear in the resume picker by default).

## Costs

Speech recognition and speech are free (on your server). The brain is billed by its
provider per token: with Claude Haiku 4.5 ($1 / $5 per million tokens) a few-minute call
costs about a cent or two. The work itself runs on your Claude Code subscription.

## Status

Built and tested with stand-ins for the phone line, Whisper, Piper and the LLM (no real
model could be downloaded in the build environment). Your first call is the first real
test of speech recognition quality, voice, latency and the brain's behaviour.

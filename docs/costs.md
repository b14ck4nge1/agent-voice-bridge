# Costs

Software and running costs are listed separately. Prices for third-party services
change; check them yourself before relying on them.

| Item | Default choice | Cost |
|---|---|---|
| This project | MIT | Free |
| iPhone app | Linphone (App Store) | Free app |
| Apple Developer Program | not needed (no custom iOS app) | 0 |
| SIP accounts (×2) | sip.linphone.org free service | 0. Free community service without guaranteed support; terms for automated use unverified. |
| Push notifications | Provided by sip.linphone.org for its own accounts | 0. For a self-hosted SIP server, Belledonne sells push as a paid offering. |
| Phone number / PSTN / SIP trunk | not used | 0 |
| Public server / tunnel | not needed (outbound connections only) | 0, *if HW-01/HW-02 confirm no relay is needed* |
| TURN relay | only if cellular NAT blocks direct media | 0 self-hosted on hardware you own; otherwise a small VPS or a TURN service's free tier |
| Speech-to-text | local `faster-whisper` (MIT) on your PC | 0, uses CPU/GPU |
| Text-to-speech | local Kokoro (Apache-2.0) or Windows SAPI | 0 |
| Conversation LLM | rule-based engine (built in) | 0. Optional cloud LLMs (Anthropic, OpenAI) bill per token. |
| Realtime voice APIs (optional) | OpenAI Realtime, Deepgram, ... | Per-minute charges |
| Your data plan | iPhone cellular data during calls | G.711 is about 90 kbit/s each way including overhead (~1.3 MB per minute in total) |

**Expected running cost of the default setup: 0**, plus electricity and mobile data.
That depends on the free sip.linphone.org service staying available to you.

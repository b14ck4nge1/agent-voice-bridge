# Phone approvals for Claude Code cloud sessions

Claude Code sessions in the cloud (claude.ai/code, the Claude app) run the hooks that a
repository commits in `.claude/settings.json`. With the Agent Voice Bridge hook in a
repository, a cloud session on it calls your phone when it needs permission, just like
Claude Code on your PC.

## 1. Add the hook to each repository (on your PC)

```powershell
cd path\to\your-repo
& "C:\Users\kianc\Desktop\New folder\AI Shop\SIP\agent-voice-bridge\.venv\Scripts\avb-admin.exe" install-cloud-hook --repo .
git add .claude && git commit -m "Phone approvals for cloud sessions" && git push
```

This copies one file (`.claude/hooks/avb-hook.py`, standard library only) and adds it to
`.claude/settings.json` (other settings are kept). It does nothing on your PC: it only
acts where `AVB_AGENT_TOKEN` is set as an environment variable, so your PC's own hook keeps
deciding there. This repository (agent-voice-bridge) already has it.

## 2. Set up the cloud environment (once)

In the Claude app or claude.ai/code: the cloud environment menu in the session's title bar,
then **Edit**:

* **Environment variables:** `AVB_BRIDGE_URL=https://voice.n8nweb.ir` and
  `AVB_AGENT_TOKEN=<the agent token from the server's SETUP-DETAILS.txt>`.
* **Network access:** add `voice.n8nweb.ir` under *Allowed domains* (keep *Allow package
  managers* ticked).

Start a **new** session afterwards (settings and hooks load when a session starts).

## How it behaves

* File edits in cloud sessions don't ask for permission, so they don't call. Commands and
  other tools do, unless the session runs in *Auto* mode or a rule allows them.
* If the server can't be reached or nobody answers, the request is denied (the session
  can ask again).
* Anyone who can open your cloud sessions' environment settings can see the token; it is
  the same agent token your PC uses. Revoking it means generating a new one on the server.

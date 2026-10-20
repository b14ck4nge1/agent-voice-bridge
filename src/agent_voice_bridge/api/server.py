"""``avb-bridge``: run the approval bridge with uvicorn."""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from agent_voice_bridge.api.app import create_app
from agent_voice_bridge.config import Settings, default_data_dir, load_env_file
from agent_voice_bridge.sip.adapter.base import LoggingDispatcher

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="avb-bridge", description="Run the Agent Voice Bridge API"
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    load_env_file(default_data_dir() / "bridge.env")
    settings = Settings.from_env()
    assistant = None
    if settings.call_dispatcher != "log":
        from agent_voice_bridge.assistant.runtime import AssistantRuntime, assistant_enabled

        if assistant_enabled():
            assistant = AssistantRuntime()
            log.info("AI assistant on: brain %s", os.environ.get("AVB_BRAIN", "anthropic"))
    hub = assistant.worker if assistant else None
    if settings.call_dispatcher == "log":
        app = create_app(settings, dispatcher=LoggingDispatcher())
    elif settings.call_dispatcher == "bark-sip":
        from agent_voice_bridge.phone import PhoneStack
        from agent_voice_bridge.phone_config import PhoneSettings

        try:
            phone = PhoneStack(PhoneSettings.from_env(), assistant=assistant)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        app = create_app(
            settings, dispatcher=phone.call_dispatcher, extension=phone.run, worker_hub=hub
        )
    elif settings.call_dispatcher == "linphone":
        from agent_voice_bridge.linphone import LinphoneSettings, LinphoneStack

        try:
            stack = LinphoneStack(LinphoneSettings.from_env(), assistant=assistant)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        app = create_app(
            settings, dispatcher=stack.call_dispatcher, extension=stack.run, worker_hub=hub
        )
    else:
        raise SystemExit(
            f"Unknown AVB_CALL_DISPATCHER={settings.call_dispatcher!r} "
            "(use log, bark-sip or linphone)"
        )
    uvicorn.run(
        app, host=settings.host, port=settings.port, log_level=args.log_level, access_log=False
    )


if __name__ == "__main__":
    main()

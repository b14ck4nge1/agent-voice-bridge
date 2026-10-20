"""``avb-bridge``: run the approval bridge with uvicorn."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from agent_voice_bridge.api.app import create_app
from agent_voice_bridge.config import Settings, default_data_dir, load_env_file
from agent_voice_bridge.sip.adapter.base import LoggingDispatcher


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
    if settings.call_dispatcher != "log":
        raise SystemExit(
            f"AVB_CALL_DISPATCHER={settings.call_dispatcher!r} is not available yet; "
            "only 'log' ships in this release (see PROJECT_STATUS.md)"
        )
    app = create_app(settings, dispatcher=LoggingDispatcher())
    uvicorn.run(
        app, host=settings.host, port=settings.port, log_level=args.log_level, access_log=False
    )


if __name__ == "__main__":
    main()

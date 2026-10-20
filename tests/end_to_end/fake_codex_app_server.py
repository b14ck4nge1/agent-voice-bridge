"""A stand-in for ``codex app-server`` that speaks the message shapes from
openai/codex ``codex-rs/app-server-protocol`` (no "jsonrpc" field, one JSON object
per line). It asks for one file-change and one command approval, then reports the
client's answers in a ``turn/completed`` notification.
"""

import json
import sys


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    answers: dict = {}
    saw_jsonrpc_field = False
    for line in sys.stdin:
        msg = json.loads(line)
        saw_jsonrpc_field = saw_jsonrpc_field or "jsonrpc" in msg
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            send({"id": mid, "result": {"userAgent": "fake-codex/0.0"}})
        elif method == "thread/start":
            assert msg["params"]["approvalPolicy"] == "on-request"
            send(
                {
                    "id": mid,
                    "result": {"thread": {"id": "thr_1"}, "model": "fake", "modelProvider": "fake"},
                }
            )
        elif method == "turn/start":
            send({"id": mid, "result": {"turn": {"id": "turn_1", "status": "inProgress"}}})
            send(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "startedAtMs": 0,
                        "item": {
                            "type": "fileChange",
                            "id": "item_fc",
                            "status": "inProgress",
                            "changes": [
                                {
                                    "path": "src/app.py",
                                    "kind": {"type": "update", "move_path": None},
                                    "diff": "@@ -1 +1 @@\n-print(1)\n+print(2)\n",
                                }
                            ],
                        },
                    },
                }
            )
            send(
                {
                    "id": 0,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "itemId": "item_fc",
                        "startedAtMs": 0,
                        "reason": "edit app",
                        "grantRoot": None,
                    },
                }
            )
            send(
                {
                    "id": "srv-cmd",
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thr_1",
                        "turnId": "turn_1",
                        "itemId": "item_cmd",
                        "startedAtMs": 0,
                        "command": "rm -rf build",
                        "cwd": "/tmp/ws",
                        "reason": "clean build output",
                    },
                }
            )
        elif mid is not None and method is None:
            answers[str(mid)] = msg.get("result", {}).get("decision")
            if len(answers) == 2:
                send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thr_1",
                            "turn": {
                                "id": "turn_1",
                                "status": "completed",
                                "answers": answers,
                                "sawJsonrpcField": saw_jsonrpc_field,
                            },
                        },
                    }
                )


if __name__ == "__main__":
    main()

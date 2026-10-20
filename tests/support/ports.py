"""Test helper: free local ports."""

from __future__ import annotations

import random
import socket


def free_port() -> int:
    """A port free for both TCP and UDP.

    Picked from 20000-39999, outside the OS's ephemeral range: on Windows the
    ephemeral TCP ports the OS hands out often fall in reserved UDP ranges.
    """
    for _ in range(500):
        port = random.randint(20000, 39999)
        with socket.socket() as t, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            try:
                t.bind(("127.0.0.1", port))
                u.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")

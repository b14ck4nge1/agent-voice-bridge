# Contributing

1. Python 3.11+. `python -m venv .venv` and `pip install -e ".[dev]"`.
2. Before a PR: `ruff check src tests`, `ruff format --check src tests`, `mypy`,
   `pytest` (or `scripts\test.ps1` on Windows).
3. Keep Windows a first-class platform: no POSIX-only calls without a Windows path,
   no shell-dependent hook commands.
4. Security-relevant changes (anything touching `approvals/`, `security/`,
   `agents/hook.py` or the conversation decision path) need tests that show the
   fail-closed behaviour.
5. Don't paste code from GPL-licensed or unlicensed projects. See
   [docs/research.md](docs/research.md) for which referenced projects may be reused.
6. Never claim hardware results (calls, audio, lock-screen behaviour) in docs or PRs
   without a device test report.

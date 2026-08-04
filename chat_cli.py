"""Terminal REPL for the MCB Agent.

Usage:
  python chat_cli.py                # start REPL
  python chat_cli.py "your question" # single-shot mode

Commands inside REPL:
  /reload    re-load announcements + events from disk
  /context   print how many announcements/events are loaded
  /reset     clear conversation history
  /quit      exit
"""
from __future__ import annotations

import sys

from mcb_digest.agent import Agent


BANNER = """MCB Agent — local REPL. Type /quit to exit, /reload to refresh, /context for a summary.
"""


def _print_context(agent: Agent) -> None:
    print("Loaded context:")
    for name, anns in agent.ctx.per_child.items():
        print(f"  {name}: {len(anns)} announcement(s)")
    print(f"  events: {len(agent.ctx.events)} total, {len(agent.ctx.upcoming)} upcoming")


def _interactive(agent: Agent) -> int:
    print(BANNER)
    _print_context(agent)
    while True:
        try:
            q = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not q:
            continue
        if q in {"/quit", "/exit"}:
            return 0
        if q == "/reload":
            agent.reload_context()
            _print_context(agent)
            continue
        if q == "/context":
            _print_context(agent)
            continue
        if q == "/reset":
            agent.reset()
            print("(history cleared)")
            continue
        try:
            answer = agent.ask(q)
        except Exception as e:
            print(f"[error] {e}")
            continue
        print(f"\nagent> {answer}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    agent = Agent()
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        print(agent.ask(q))
        return 0
    return _interactive(agent)


if __name__ == "__main__":
    sys.exit(main())

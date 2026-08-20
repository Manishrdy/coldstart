"""Process exit codes shared by the CLI entrypoints and the daemon.

Every entrypoint used to return 1 for every distinct failure, which left the
daemon (Module 20) unable to tell "out of budget" from "broken .env" — and
those need opposite reactions: one suspends polling until the next local
midnight, the other just needs a loud log while the loop keeps ticking so an
edited .env is picked up on the next spawn.

Every failure code is still non-zero, so cron's mail-on-error behaviour (and
anything else reading "did it work?") is unaffected.
"""

from __future__ import annotations

OK = 0
FAILURE = 1  # unexpected / unhandled
BUDGET_EXCEEDED = 2
RESUMES_NOT_READY = 3
CONFIG_ERROR = 4

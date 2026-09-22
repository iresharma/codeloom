This repo is the engine you are currently running as — you are being asked to
improve your own codebase. This is a two-part task; land it as a single PR.

## Part A — robust HTTP/HTTPS error handling

Find the `http_request` tool (under `tools/`). Harden it against real-world
failure modes that a naive implementation misses:

- Connection timeouts and read timeouts, with sane, configurable defaults.
- TLS/certificate errors surfaced as a clear, actionable error string rather
  than a raw stack trace.
- Redirect handling (including redirect loops).
- Connection resets / DNS failures.
- Retry with backoff for transient failures (timeouts, 5xx) — but do not retry
  non-idempotent methods or 4xx responses.

Every failure mode should degrade to a clear `error: ...` string the calling
model can read and react to, matching the existing convention in this
codebase where tool exceptions become readable error strings instead of
aborting the turn. Add or extend tests under `tests/` covering these failure
paths (mock the transport, don't hit the real network in tests).

## Part B — investigate packaging the engine as a deployable binary

Right now this is a Python project run via `python app.py` / `python
dummy_client.py` inside a venv. Investigate turning it into a single
deployable binary (PyInstaller, Nuitka, and shiv/zipapp are the obvious
candidates — pick one and justify the choice).

Scope this realistically: you do not need to solve full packaging in one PR.
Produce:

1. A working build for the core server + client path (`app.py`, headless
   client) using your chosen tool, checked into the repo as a build script
   (e.g. `scripts/build_binary.sh` or similar) that someone else can re-run.
2. A short written doc (e.g. `docs/packaging.md`) explaining what does *not*
   survive naive bundling and why: tree-sitter grammar packages, `npx`-spawned
   language servers (pyright, typescript-language-server), `gopls`, and
   Playwright's browser binaries are the known trouble spots — investigate
   each rather than assuming, and write down what you actually found. State
   clearly what a *full* solution would require, as a scoped follow-up plan,
   rather than trying to solve all of it here.

When you are done, close with a single clear paragraph summarizing exactly
what you changed (both parts) — that summary becomes the pull request's title
and body verbatim, so make it read like a real PR description. Be explicit in
that summary about what Part B's prototype does and does not cover.

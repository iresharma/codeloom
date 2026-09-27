# codeloom

Umbrella repo for the codeloom projects. Each project lives in its own GitHub
repo (`codeloom.<name>`) and is mounted here as a submodule under `workspace/`.

| Path | Repo | What it is |
|---|---|---|
| `workspace/engine` | `codeloom.engine` | Headless coding-agent backend (JSON protocol over a Unix socket) |
| `workspace/TUI` | `codeloom.TUI` | Terminal client for the engine |
| `workspace/pages` | `codeloom.pages` | Project site and engine pages |
| `workspace/experiments` | `codeloom.experiments` | Experiment write-ups |
| `workspace/cloud-controller` | `codeloom.cloud-controller` | Cloud controller |

Clone with submodules:

```bash
git clone --recurse-submodules git@github.com:iresharma/codeloom.git
# or, in an existing checkout
git submodule update --init --recursive
```

## Scripts

| Script | Does |
|---|---|
| `scripts/new <name> [--public]` | Create `workspace/<name>`, init it, and create the `codeloom.<name>` GitHub repo |
| `scripts/sync-workspace` | Record inner repos under `workspace/` as submodules (or track their files if they have no origin) |
| `scripts/pull [<name> \| .]` | Pull every inner repo then the outer one, or just one of them |
| `scripts/commit <message>` | Commit and push every inner repo, then the outer one, with one message |
| `scripts/install-hooks` | Point git at `.githooks/` (pre-commit runs `sync-workspace`; commit-msg strips Cursor trailers) |

`scripts/git-identity.sh` resolves the human author the other scripts commit as.

API keys go in an untracked `env.sh` (see `workspace/engine/README.md` for the
variables the engine reads).

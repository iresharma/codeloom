This repo's own README, in the "Status" section, flags a real gap: "there's no
authentication in front of the UI or API, so don't expose the collector
Service outside the cluster without putting your own auth in front of it."

Fix that. Add authentication in front of the collector's HTTP surface:

- The JSON/query API under `internal/collector/queryapi` and
  `internal/collector/ingest`.
- The server-rendered HTMX/Alpine UI under `internal/collector/ui`.

Use a simple, self-hosted scheme appropriate for a homelab project — a
token or basic-auth credential supplied via config/env, checked in
middleware — not an external OAuth provider. Wire it through the same
config mechanism the rest of the collector's settings already use (check how
existing ConfigMap-driven settings are read). Make sure the agent-to-collector
ingest path (`POST /api/v1/logs`) still works — that's service-to-service, not
a browser, so it likely needs its own credential rather than sharing the UI's.

This repo has real test coverage and a real convention for it. Match that
convention:

- Add table-driven `_test.go` files for the new middleware/auth logic, in the
  same style as the existing tests in `internal/collector/*`.
- Run `go test ./...`, `make vet`, and `make fmt` before finishing, and make
  sure all three are clean — don't declare the task done otherwise.
- Update `deploy/k8s/*.yaml` and the README/config docs if the new auth
  setting needs to be provided at deploy time.

When you are done, close with a single clear paragraph summarizing exactly
what you changed and why — that summary becomes the pull request's title and
body verbatim, so make it read like a real PR description, not an internal
note.

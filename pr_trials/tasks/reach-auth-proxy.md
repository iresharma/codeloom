Implement GitHub issue #9 on this repo: "add redis caching in auth proxy for kanban endpoints."

There is already a shared Redis client at `internal/pkg/redis/main.go`, and it is
already used elsewhere in the codebase (see `internal/app/server.go` and
`internal/pkg/server/permissions/perm.go` for the existing usage pattern). Follow
that same pattern rather than inventing a new one.

Scope:
- Add caching for the read paths in `internal/pkg/server/routes/kanban.go`.
- Add cache invalidation on the corresponding write paths, so a write never
  leaves a stale cached read behind.
- Pick sane key naming and TTLs consistent with how the existing Redis usage in
  this repo does it.
- This repo currently has no test files. Do not feel obligated to introduce a
  whole test framework for this change, but do double check the code compiles
  and, if there's an easy way to sanity-check the caching behavior manually
  (e.g. a local run), do that.

When you are done, close with a single clear paragraph summarizing exactly what
you changed and why — that summary becomes the pull request's title and body
verbatim, so make it read like a real PR description, not an internal note.

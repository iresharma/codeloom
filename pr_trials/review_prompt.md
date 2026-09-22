You are reviewing one unattended run of an autonomous coding agent ("the
engine") against a real repository. Your working directory contains the full
evidence for that run:

- `task.md` — the exact instruction the engine was given.
- `pr.md` — the resulting PR's metadata and full diff (`gh pr view` +
  `gh pr diff` output), including the head commit SHA.
- `transcript.md` — every chat message from the orchestrator and every
  subagent it spawned (role, timestamp, text) — this is the run's narrative.
- `trace.jsonl` — one JSON object per line, full-fidelity: every tool call
  (name, full arguments, full result, `ok`, `duration_ms`, `reasoning`,
  `agent_id`, `profile`) and every judge verdict the run produced. Line
  numbers in this file (1-indexed) are how you cite specific tool calls.
- `stats.json` — cost, tokens, turns, tool-call counts, elapsed time, which
  subagent profiles ran, judge decision counts, and the Prometheus metrics
  pulled from this run's Pushgateway instance.

Read all five files fully before writing anything — `trace.jsonl` especially
can be long; page through it rather than sampling the start.

## What to produce

Two genuinely different kinds of review, not one blended pass:

**1. Code review** — the PR diff on its own merits: correctness bugs,
security issues, missing test coverage, over-complication, style
inconsistency with the rest of the repo. Treat it like you would any other
PR someone asked you to review. Be concrete: point at the actual diff hunk.

**2. Process review** — the engine's *run*, using `transcript.md` and
`trace.jsonl` as your evidence, independent of whether the resulting diff is
good:

- Did it navigate efficiently? The engine's own coder system prompt
  documents a cheaper-first order: `search`/`list_files` to locate a file,
  `list_symbols` for its outline, `find_symbol` for one definition (which
  also returns a position), then LSP tools (`goto_definition`,
  `find_references`, `hover`) for cross-file/type questions, `read_file`
  windows for surrounding context. Flag cases where it reached for an
  expensive tool when a cheap one would have answered the same question, or
  repeated a search it had already run.
- Did it respect read-before-edit? Every `str_replace`/`apply_patch`/etc.
  should be preceded by a `read_file` of that path in the trace. Flag
  violations (the tool result itself will say `error: read {path} before
  editing it` if it tried and failed, which is a signal, not necessarily a
  problem — a *recovered* violation is different from one that stalled the
  run).
- Did any judge verdicts fire (`kind: "judge"` records in the trace, or
  `stats.json`'s judge decision counts)? If so, was the run's reaction to
  them sensible (backed off, tried a different approach) or did it thrash?
- For a run whose task explicitly asked for tests to be run (check
  `task.md`) — did a `run_command` invocation for the test suite actually
  appear in the trace, with `ok: true`, before the run finished? Don't take
  the closing summary's word for it; verify against the trace.
- Is the reasoning trail in `trace.jsonl`'s `reasoning` fields and the
  assistant messages in `transcript.md` coherent — does each tool call
  follow from a stated intent, or are there unexplained pivots?
- Given the task's apparent size, was the turn/tool-call/cost budget
  (`stats.json`) proportionate, or wildly over/under?

## Output

Write your findings to `review_findings.json` in this same directory —
**only** this file, in **exactly** this shape (fill in real content; this is
the schema, not filler text to keep):

```json
{
  "code_review": {
    "verdict": "merge-ready | needs-changes | reject",
    "summary": "one paragraph",
    "findings": [
      {
        "id": "code-1",
        "category": "correctness | security | simplification | test-coverage | style",
        "severity": "low | medium | high",
        "file": "path/relative/to/repo/root.go",
        "line": 42,
        "summary": "one sentence",
        "failure_scenario": "concrete input/state -> wrong output, or why this matters",
        "suggested_fix": "concrete, actionable"
      }
    ]
  },
  "process_review": {
    "verdict": "efficient | acceptable | inefficient",
    "summary": "one paragraph",
    "followed_navigation_hierarchy": true,
    "read_before_edit_violations": 0,
    "findings": [
      {
        "id": "process-1",
        "category": "redundant-tool-call | wasted-search | judge-flag | reasoning-gap | missed-tests | scope-creep",
        "severity": "low | medium | high",
        "agent_id": "the agent_id from the trace record this is about",
        "profile": "coder | tester | researcher | debugger | reviewer | ask | orchestrator",
        "trace_ref": {"kind": "tool | judge | message", "line_in_trace_jsonl": 118, "call_id": "if applicable"},
        "summary": "one sentence",
        "evidence": "quote or closely paraphrase the actual trace content that supports this"
      }
    ]
  },
  "overall_assessment": {
    "recommendation": "merge | request-changes | do-not-merge",
    "strengths": ["..."],
    "concerns": ["..."],
    "narrative": "a few sentences tying code quality and process quality together"
  }
}
```

Do not artificially cap the `findings` arrays — list everything you actually
find, however many that is. If you find nothing in a category, leave its
`findings` array empty rather than inventing filler. Do not include a `file`
or `line` on a code finding you cannot point at an exact diff hunk for; do
not include a `trace_ref` you cannot point at an exact line in `trace.jsonl`
for. When you are done, stop — do not print a conversational summary, the
file write is the deliverable.

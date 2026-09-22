"""Reconstruct the decision flow of one engine run: which agents it spawned,
why, what each one actually did, and how it ended (worktree settle / PR).

Parsing approach adapted from workspace/engine/scripts/bench_flow.py (the
engine's own A/B flow-report tool) trimmed to a single side: dummy_client.py's
formatted stdout (saved as <workdir>/client.log by run_target.py) is the
primary source for agent boundaries and judge verdicts - it already carries
`agent started <profile> <id> ... task=...` / `agent finished <profile> <id>
<status>: ...` lines and `judge <tag> -> <verdict> (...) [<agent_id>]: ...`
lines with exact agent attribution. <workdir>/transcript.md supplies each
child's compressed narrative report (the `[agent <profile> <id8> finished]`
blocks with status/summary/outcome/facts/leftover fields) and the final
`[worktree <profile> <id8> <action>]` settle event. <workdir>/repo/.engine/
trace.jsonl (ENGINE_TRACE_CALLS=1) enriches both with full call detail -
tool args/result/reasoning by agent_id, judge request/response by (tag,
order) since judge trace records carry no agent_id of their own.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_JUDGE_RE = re.compile(
    r"^judge (?P<kind>\S+) -> (?P<verdict>\S+) \((?P<mode>enforced|advisory), (?P<ms>\d+)ms\)"
    r"(?: \[(?P<agent>[0-9a-f]{6,40})\])?: (?P<subject>.*)$"
)
_TOOL_RE = re.compile(
    r"^tool (?P<name>\S+) (?P<state>started|ok|error)(?: \((?P<ms>\d+)ms\))?"
    r"(?: \[(?P<agent>[0-9a-f]{6,40})\])?"
)
_AGENT_STARTED_RE = re.compile(
    r"^agent started (?P<profile>\S+) (?P<id>[0-9a-f]{8,40}) batch=(?P<batch>.*?) task=(?P<task>.*?)"
    r"(?: worktree=(?P<worktree>\S+) branch=(?P<branch>\S+))?$"
)
_AGENT_FINISHED_RE = re.compile(
    r"^agent finished (?P<profile>\S+) (?P<id>[0-9a-f]{8,40}) (?P<status>[a-zA-Z_]+):"
    r"(?: \$(?P<cost>[0-9.]+) (?P<tokens>\d+) tok (?P<cached>\d+) cached)? ?(?P<rest>.*)$"
)

# transcript.md headers carry a timestamp ("## engine (2026-...)") that
# bench_flow.py's plain "## role" blocks don't have - tolerate it here.
_BLOCK_RE = re.compile(r"^## (\w+)(?: \(.*\))?\s*$", re.M)
_TAG_RE = re.compile(r"^\[(.*?)\]\n?(.*)", re.S)
_FIELD_LABELS = ("status", "summary", "what", "paths", "facts", "outcome",
                  "verdict", "leftover_questions", "leftover", "files_touched")
_FIELD_RE = re.compile(r"(?m)^(" + "|".join(_FIELD_LABELS) + r"):[ \t]*")
_URL_RE = re.compile(r"https?://\S+")
_SPAWN_AGENT_ID_RE = re.compile(r"agent_id=([0-9a-f]{8,40})")

PERSONALITIES = {"ask", "coder", "tester", "researcher", "debugger", "reviewer"}


@dataclass
class JudgeCall:
    kind: str
    verdict: str
    mode: str
    ms: int
    subject: str
    response: dict[str, Any] | None = None


@dataclass
class ToolCall:
    name: str
    state: str
    ms: int | None
    arguments: dict[str, Any] | None = None
    result: str = ""
    reasoning: str = ""
    ok: bool | None = None
    ts: float | None = None


@dataclass
class AgentPhase:
    profile: str
    agent_id: str
    task_preview: str
    branch: str = ""
    status: str = ""
    cost: float = 0.0
    tokens: int = 0
    summary: str = ""
    spawn_reasoning: str = ""
    fields: dict[str, list[str]] = field(default_factory=dict)
    judge_calls: list[JudgeCall] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    start_ts: float | None = None
    end_ts: float | None = None

    def field(self, *names: str) -> str:
        for name in names:
            values = self.fields.get(name)
            if values:
                return values[-1]
        return ""

    def tool_tally(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for c in self.tool_calls:
            tally[c.name] = tally.get(c.name, 0) + 1
        return tally


@dataclass
class SettleEvent:
    profile: str
    action: str
    detail: str
    pr_url: str = ""


@dataclass
class Flow:
    prompt: str = ""
    orchestrator_judge: list[JudgeCall] = field(default_factory=list)
    orchestrator_tools: list[ToolCall] = field(default_factory=list)
    phases: list[AgentPhase] = field(default_factory=list)
    settle: SettleEvent | None = None
    final_reply: str = ""
    has_trace: bool = False


def _split_fields(body: str) -> dict[str, list[str]]:
    matches = list(_FIELD_RE.finditer(body))
    out: dict[str, list[str]] = defaultdict(list)
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        out[m.group(1)].append(body[start:end].strip())
    return dict(out)


def parse_client_log(path: Path) -> tuple[str, list[JudgeCall], list[ToolCall], list[AgentPhase]]:
    text = path.read_text(errors="replace") if path.exists() else ""
    lines = text.split("\n")
    prompt = ""
    orch_judge: list[JudgeCall] = []
    orch_tools: list[ToolCall] = []
    phases: list[AgentPhase] = []
    by_id: dict[str, AgentPhase] = {}

    for line in lines:
        if not prompt and line.startswith("user: "):
            prompt = line[len("user: "):].strip()
            continue

        m = _JUDGE_RE.match(line)
        if m:
            call = JudgeCall(kind=m.group("kind"), verdict=m.group("verdict"),
                              mode=m.group("mode"), ms=int(m.group("ms")), subject=m.group("subject"))
            target = by_id.get(m.group("agent")) if m.group("agent") else None
            (target.judge_calls if target else orch_judge).append(call)
            continue

        m = _TOOL_RE.match(line)
        if m:
            call = ToolCall(name=m.group("name"), state=m.group("state"),
                             ms=int(m.group("ms")) if m.group("ms") else None)
            target = by_id.get(m.group("agent")) if m.group("agent") else None
            (target.tool_calls if target else orch_tools).append(call)
            continue

        m = _AGENT_STARTED_RE.match(line)
        if m:
            phase = AgentPhase(profile=m.group("profile"), agent_id=m.group("id"),
                                task_preview=m.group("task"), branch=m.group("branch") or "")
            phases.append(phase)
            by_id[phase.agent_id] = phase
            continue

        m = _AGENT_FINISHED_RE.match(line)
        if m:
            phase = by_id.get(m.group("id"))
            if phase is not None:
                phase.status = m.group("status")
                phase.cost = float(m.group("cost") or 0.0)
                phase.tokens = int(m.group("tokens") or 0)
                phase.summary = m.group("rest") or ""

    return prompt, orch_judge, orch_tools, phases


def parse_transcript(path: Path, phases_by_id8: dict[str, AgentPhase]) -> tuple[SettleEvent | None, str]:
    text = path.read_text(errors="replace") if path.exists() else ""
    if not text.strip():
        return None, ""
    matches = list(_BLOCK_RE.finditer(text))
    settle: SettleEvent | None = None
    last_assistant_text = ""
    for idx, m in enumerate(matches):
        role = m.group(1)
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if role == "assistant":
            last_assistant_text = body
            continue
        if role != "engine":
            continue
        tag_match = _TAG_RE.match(body)
        if not tag_match:
            continue
        tag, rest = tag_match.group(1), tag_match.group(2)
        tokens = tag.split()
        if tokens[0] == "agent" and len(tokens) >= 3:
            phase = phases_by_id8.get(tokens[2])
            if phase is not None:
                phase.fields = _split_fields(rest)
            continue
        if tokens[0] == "worktree" and len(tokens) >= 4:
            profile, action = tokens[1], tokens[3]
            url_match = _URL_RE.search(rest)
            pr_url = url_match.group(0).rstrip(".,;:!?*") if url_match else ""
            settle = SettleEvent(profile=profile, action=action, detail=rest.strip(), pr_url=pr_url)
    return settle, last_assistant_text


def parse_trace(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def correlate(flow: Flow, trace_records: list[dict]) -> None:
    """Enrich judge/tool calls with full trace detail; attach spawn reasoning."""
    by_agent_tools: dict[str, list[dict]] = defaultdict(list)
    judge_by_tag: dict[str, list[dict]] = defaultdict(list)
    for r in trace_records:
        if r.get("kind") == "tool":
            by_agent_tools[r.get("agent_id") or ""].append(r)
        elif r.get("kind") == "judge":
            judge_by_tag[r.get("tag", "")].append(r)

    def enrich_tools(calls: list[ToolCall], records: list[dict]) -> None:
        # both lists are in file/chronological order; only "ok"/"error" states
        # (not "started" duplicates) carry full detail in the trace.
        finished = [c for c in calls if c.state != "started"]
        for call, rec in zip(finished, records):
            call.arguments = rec.get("arguments")
            call.result = str(rec.get("result", ""))
            call.reasoning = str(rec.get("reasoning", ""))
            call.ok = rec.get("ok")
            call.ts = rec.get("ts")

    def enrich_judges(calls: list[JudgeCall], tag_pool: dict[str, list[dict]], consumed: dict[str, int]) -> None:
        for call in calls:
            pool = tag_pool.get(call.kind, [])
            i = consumed.get(call.kind, 0)
            if i < len(pool):
                call.response = pool[i].get("response")
                consumed[call.kind] = i + 1

    enrich_tools(flow.orchestrator_tools, by_agent_tools.get("", []))
    consumed: dict[str, int] = {}
    enrich_judges(flow.orchestrator_judge, judge_by_tag, consumed)
    for phase in flow.phases:
        enrich_tools(phase.tool_calls, by_agent_tools.get(phase.agent_id, []))
        enrich_judges(phase.judge_calls, judge_by_tag, consumed)
        agent_tools = by_agent_tools.get(phase.agent_id, [])
        if agent_tools:
            phase.start_ts = agent_tools[0].get("ts")
            phase.end_ts = agent_tools[-1].get("ts")

    # orchestrator's own spawn calls (name == a personality) carry, in their
    # trace "result" string, "started agent_id=<id> ..." - and sometimes a
    # "reasoning" field stating *why* it's spawning this next. Match by
    # agent_id extracted from that result string, not by order (orchestrator
    # spawn tool calls can interleave with other orchestrator tool calls).
    phases_by_id = {p.agent_id: p for p in flow.phases}
    for r in by_agent_tools.get("", []):
        if r.get("name") not in PERSONALITIES:
            continue
        m = _SPAWN_AGENT_ID_RE.search(str(r.get("result", "")))
        if not m:
            continue
        phase = phases_by_id.get(m.group(1))
        if phase is not None:
            phase.spawn_reasoning = str(r.get("reasoning", "")).strip()
            if not phase.task_preview or len(phase.task_preview) < 20:
                args = r.get("arguments") or {}
                phase.task_preview = str(args.get("task", phase.task_preview))


def build_flow(workdir: Path) -> Flow:
    prompt, orch_judge, orch_tools, phases = parse_client_log(workdir / "client.log")
    flow = Flow(prompt=prompt, orchestrator_judge=orch_judge, orchestrator_tools=orch_tools, phases=phases)

    phases_by_id8 = {p.agent_id[:8]: p for p in phases}
    settle, final_reply = parse_transcript(workdir / "transcript.md", phases_by_id8)
    flow.settle = settle
    flow.final_reply = final_reply

    trace_records = parse_trace(workdir / "repo" / ".engine" / "trace.jsonl")
    flow.has_trace = bool(trace_records)
    if trace_records:
        correlate(flow, trace_records)

    return flow

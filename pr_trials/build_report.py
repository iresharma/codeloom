#!/usr/bin/env python3
"""Build one static HTML report from every JSON report in pr_trials/reports/.

Renders each PR as an actual GitHub-style diff with the review's code
findings attached as inline comments on the exact line they're about,
plus small-multiple bar charts comparing cost/tokens/tool-calls/elapsed
and findings-by-severity across runs. Colors follow the dataviz skill's
validated dark-mode palette (status colors for severity, fixed-order
categorical hues for repo identity).

    python3 pr_trials/build_report.py
    open pr_trials/report.html

Fetches each PR's current diff fresh via `gh pr diff` at build time (so
the report reflects the PR's live state, not a stale snapshot) - no other
dependencies beyond the stdlib and `gh`. Safe to rerun any time.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from flow import Flow, build_flow  # noqa: E402

PR_TRIALS = Path(__file__).resolve().parent
REPORTS_DIR = PR_TRIALS / "reports"
RUNS_DIR = PR_TRIALS / "runs"
OUT_PATH = PR_TRIALS / "report.html"

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
VERDICT_CLASS = {
    "merge-ready": "good", "efficient": "good", "merge": "good",
    "needs-changes": "warning", "acceptable": "warning", "request-changes": "warning",
    "reject": "critical", "inefficient": "critical", "do-not-merge": "critical",
}
SEV_STATUS = {"high": "critical", "medium": "warning", "low": "good"}

# dataviz skill's validated dark-mode palette (references/palette.md) - used verbatim
CAT = ["#3987e5", "#d95926", "#199e70"]  # categorical slots 1/2/3: blue, orange, aqua
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
INK = {"primary": "#ffffff", "secondary": "#c3c2b7", "muted": "#898781"}
SURFACE = {"chart": "#1a1a19", "page": "#0d0d0d", "gridline": "#2c2c2a", "baseline": "#383835"}


def e(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def blob_permalink(repo: str, sha: str, file: str, line: int | None) -> str:
    if not repo or not sha or not file:
        return ""
    url = f"https://github.com/{repo}/blob/{sha}/{urllib.parse.quote(file)}"
    return url + (f"#L{line}" if line else "")


def load_reports() -> list[dict[str, Any]]:
    reports = []
    for path in sorted(REPORTS_DIR.glob("*.json")):
        try:
            reports.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return reports


def fetch_diff(pr_url: str) -> str:
    if not pr_url:
        return ""
    result = subprocess.run(["gh", "pr", "diff", pr_url], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# unified diff -> structured hunks
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@\s?(.*)$")


def parse_diff(diff_text: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    old_no = new_no = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                files.append(current)
            current = {"path": "", "old_path": "", "additions": 0, "deletions": 0,
                       "binary": False, "hunks": []}
            hunk = None
        elif current is None:
            continue
        elif line.startswith("--- "):
            p = line[4:]
            current["old_path"] = "" if p == "/dev/null" else p.removeprefix("a/")
        elif line.startswith("+++ "):
            p = line[4:]
            current["path"] = current["old_path"] if p == "/dev/null" else p.removeprefix("b/")
        elif line.startswith("Binary files"):
            current["binary"] = True
        elif line.startswith(("index ", "new file", "deleted file", "similarity", "rename")):
            continue
        elif (m := _HUNK_RE.match(line)):
            old_no, new_no = int(m.group(1)), int(m.group(2))
            hunk = {"header": m.group(3), "range": line.split("@@")[1].strip(), "lines": []}
            current["hunks"].append(hunk)
        elif hunk is not None:
            if line.startswith("+"):
                hunk["lines"].append({"type": "add", "old": None, "new": new_no, "text": line[1:]})
                new_no += 1
                current["additions"] += 1
            elif line.startswith("-"):
                hunk["lines"].append({"type": "del", "old": old_no, "new": None, "text": line[1:]})
                old_no += 1
                current["deletions"] += 1
            elif line.startswith("\\"):
                continue
            else:
                hunk["lines"].append({"type": "ctx", "old": old_no, "new": new_no, "text": line[1:]})
                old_no += 1
                new_no += 1
    if current is not None:
        files.append(current)
    return files


def extract_snippet(files: list[dict], path: str, line: int | None, context: int = 2) -> list[dict] | None:
    """A few lines of the diff around `line` (new-file numbering) - not the whole file."""
    if line is None:
        return None
    for file in files:
        if (file["path"] or file["old_path"]) != path:
            continue
        for hunk in file["hunks"]:
            idx = next((i for i, ln in enumerate(hunk["lines"]) if ln["new"] == line), None)
            if idx is None:
                continue
            lo, hi = max(0, idx - context), min(len(hunk["lines"]), idx + context + 1)
            return hunk["lines"][lo:hi]
    return None


def render_snippet(lines: list[dict]) -> str:
    rows = []
    for ln in lines:
        cls = {"add": "line-add", "del": "line-del", "ctx": "line-ctx"}[ln["type"]]
        sign = {"add": "+", "del": "-", "ctx": ""}[ln["type"]]
        n = ln["new"] if ln["new"] is not None else ln["old"]
        rows.append(
            f'<tr class="{cls}"><td class="gutter">{n}</td>'
            f'<td class="code"><span class="sign">{sign}</span>{e(ln["text"])}</td></tr>'
        )
    return f'<table class="snippet"><tbody>{"".join(rows)}</tbody></table>'


def render_code_findings(diff_text: str, findings: list[dict], repo: str, sha: str) -> str:
    if not findings:
        return '<p class="empty">No code findings.</p>'
    files = parse_diff(diff_text)
    rows = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity", "low"), 3))
    out = ['<div class="cards">']
    for f in rows:
        status = SEV_STATUS.get(f.get("severity", "low"), "good")
        path, line = f.get("file", ""), f.get("line")
        link = blob_permalink(repo, sha, path, line)
        loc = f'{path}:{line}' if line else path
        loc_html = f'<a href="{e(link)}" target="_blank">{e(loc)} ↗</a>' if link else e(loc)
        snippet = extract_snippet(files, path, line)
        snippet_html = render_snippet(snippet) if snippet else ""
        out.append(
            f'<div class="card status-{status}">'
            f'<div class="card-head">'
            f'<span class="dot dot-{status}"></span>'
            f'<b>{e(f.get("category", ""))}</b>'
            f'<span class="sev-tag">{e(f.get("severity", ""))}</span>'
            f'<span class="loc">{loc_html}</span>'
            f'</div>'
            f'<p>{e(f.get("summary", ""))}</p>'
            f'{snippet_html}'
            f'<details><summary>why it matters / fix</summary>'
            f'<p><b>Why it matters:</b> {e(f.get("failure_scenario", ""))}</p>'
            f'<p><b>Fix:</b> {e(f.get("suggested_fix", ""))}</p>'
            f'</details>'
            f'</div>'
        )
    out.append("</div>")
    return "".join(out)


# ---------------------------------------------------------------------------
# SVG charts (hand-rolled, dataviz skill mark specs: <=24px thick bars, 4px
# rounded data-end, hairline recessive baseline, direct end-labels, native
# <title> hover)
# ---------------------------------------------------------------------------

def svg_hbar_group(title: str, unit_fmt, rows: list[tuple[str, float, str]], width: int = 460) -> str:
    """rows: [(label, value, color)]. unit_fmt(value) -> display string."""
    bar_h, gap, label_w, pad_top = 22, 10, 96, 6
    max_v = max((v for _, v, _ in rows), default=0) or 1
    plot_w = width - label_w - 70
    h = pad_top * 2 + len(rows) * (bar_h + gap) - gap
    bars = []
    for i, (label, value, color) in enumerate(rows):
        y = pad_top + i * (bar_h + gap)
        w = max(3, (value / max_v) * plot_w)
        bars.append(
            f'<text x="{label_w - 8}" y="{y + bar_h / 2 + 4}" text-anchor="end" '
            f'class="chart-label">{e(label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" '
            f'fill="{color}"><title>{e(label)}: {e(unit_fmt(value))}</title></rect>'
            f'<text x="{label_w + w + 8:.1f}" y="{y + bar_h / 2 + 4}" '
            f'class="chart-value">{e(unit_fmt(value))}</text>'
        )
    baseline = f'<line x1="{label_w}" y1="{pad_top - 4}" x2="{label_w}" y2="{h - pad_top + 4}" class="chart-baseline"/>'
    return (
        f'<div class="chart"><div class="chart-title">{e(title)}</div>'
        f'<svg viewBox="0 0 {width} {h}" width="{width}" height="{h}">{baseline}{"".join(bars)}</svg></div>'
    )


def svg_severity_stack(repo_rows: list[tuple[str, str, dict, str]], width: int = 460) -> str:
    """repo_rows: [(repo_label, kind_label, {high,medium,low}, repo_color)]."""
    bar_h, gap, label_w, pad_top = 20, 8, 170, 8
    max_v = max((sum(counts.values()) for _, _, counts, _ in repo_rows), default=0) or 1
    plot_w = width - label_w - 20
    h = pad_top * 2 + len(repo_rows) * (bar_h + gap) - gap
    segs = []
    for i, (repo_label, kind_label, counts, _color) in enumerate(repo_rows):
        y = pad_top + i * (bar_h + gap)
        x = label_w
        total = sum(counts.values())
        segs.append(
            f'<text x="{label_w - 8}" y="{y + bar_h / 2 + 4}" text-anchor="end" '
            f'class="chart-label">{e(repo_label)} <tspan class="chart-sublabel">{e(kind_label)}</tspan></text>'
        )
        if total == 0:
            segs.append(f'<rect x="{x}" y="{y}" width="6" height="{bar_h}" rx="3" fill="{SURFACE["baseline"]}"/>')
            continue
        for sev in ("high", "medium", "low"):
            count = counts.get(sev, 0)
            if not count:
                continue
            w = max(0, (count / max_v) * plot_w - 2)
            status = SEV_STATUS[sev]
            segs.append(
                f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" '
                f'fill="{STATUS[status]}"><title>{sev}: {count}</title></rect>'
            )
            if w > 14:
                segs.append(
                    f'<text x="{x + w / 2:.1f}" y="{y + bar_h / 2 + 4}" text-anchor="middle" '
                    f'class="chart-seg-label">{count}</text>'
                )
            x += w + 2
    return (
        f'<div class="chart"><div class="chart-title">Findings by severity</div>'
        f'<div class="legend">'
        f'<span><i class="dot" style="background:{STATUS["critical"]}"></i>high</span>'
        f'<span><i class="dot" style="background:{STATUS["warning"]}"></i>medium</span>'
        f'<span><i class="dot" style="background:{STATUS["good"]}"></i>low</span>'
        f'</div>'
        f'<svg viewBox="0 0 {width} {h}" width="{width}" height="{h}">{"".join(segs)}</svg></div>'
    )


def severity_counts(findings: list[dict]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity", "low")
        if sev in counts:
            counts[sev] += 1
    return counts


# ---------------------------------------------------------------------------
# page assembly
# ---------------------------------------------------------------------------

def badge(text: str) -> str:
    cls = VERDICT_CLASS.get(text, "neutral")
    return f'<span class="badge b-{cls}">{e(text)}</span>'


def render_process_findings(findings: list[dict]) -> str:
    if not findings:
        return '<p class="empty">No process findings.</p>'
    rows = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity", "low"), 3))
    out = ['<div class="feed">']
    for f in rows:
        status = SEV_STATUS.get(f.get("severity", "low"), "good")
        ref = f.get("trace_ref", {}) or {}
        ref_txt = f"{ref.get('kind', '')} @ trace.jsonl:{ref.get('line_in_trace_jsonl', '?')}"
        out.append(
            f'<div class="feed-item status-{status}">'
            f'<span class="dot dot-{status}"></span>'
            f'<div class="feed-body">'
            f'<div class="feed-head"><b>{e(f.get("category", ""))}</b>'
            f'<span class="tag">{e(f.get("profile", ""))}</span>'
            f'<span class="tag mono">{e(ref_txt)}</span></div>'
            f'<p>{e(f.get("summary", ""))}</p>'
            f'<details><summary>evidence</summary><p>{e(f.get("evidence", ""))}</p></details>'
            f'</div></div>'
        )
    out.append("</div>")
    return "".join(out)


def render_verification(v: dict[str, Any]) -> str:
    """The factual build/test panel - computed by verify_build.py, not the LLM."""
    def chip(ok: bool | None, label_true: str, label_false: str, label_unknown: str) -> str:
        if ok is True:
            return f'<span class="vchip v-good">✓ {e(label_true)}</span>'
        if ok is False:
            return f'<span class="vchip v-critical">✗ {e(label_false)}</span>'
        return f'<span class="vchip v-neutral">? {e(label_unknown)}</span>'

    lang = v.get("language", "unknown")
    if lang == "unknown" or v.get("builds") is None:
        return '<div class="verify"><span class="vchip v-neutral">? verification unavailable</span></div>'

    chips = [chip(v.get("builds"), "builds", "does NOT build", "build unknown")]
    if lang == "go":
        new_vet = v.get("new_vet_issues", 0) or 0
        if new_vet:
            chips.append(f'<span class="vchip v-warning">⚠ {new_vet} new go vet issue(s)</span>')
        else:
            chips.append('<span class="vchip v-good">✓ no new go vet issues</span>')
    if v.get("has_tests"):
        summary = f" ({v['test_summary']})" if v.get("test_summary") else ""
        chips.append(chip(v.get("tests_pass"), f"tests pass{summary}", f"tests FAIL{summary}", "tests unknown"))
    else:
        chips.append('<span class="vchip v-neutral">no test suite in repo</span>')
    return f'<div class="verify"><span class="vlabel">{e(lang)}</span>{"".join(chips)}</div>'


def category_counts(findings: list[dict]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        cat = f.get("category", "other")
        counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return ""
    chips = "".join(
        f'<span class="cat-chip">{e(cat)} <b>{n}</b></span>'
        for cat, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return f'<div class="cat-counts">{chips}</div>'


def run_workdir_for(report: dict[str, Any]) -> Path | None:
    trace_file = (report.get("links", {}) or {}).get("trace_file", "")
    if not trace_file:
        return None
    # <workdir>/repo/.engine/trace.jsonl -> <workdir>
    workdir = Path(trace_file).parent.parent.parent
    return workdir if workdir.is_dir() else None


PROFILE_ICON = {
    "ask": "\U0001f4d6", "coder": "\U0001f4bb", "tester": "\U0001f9ea",
    "researcher": "\U0001f50d", "debugger": "\U0001f41b", "reviewer": "\U0001f9d0",
}
STATUS_TO_STATUS = {"ok": "good", "failed": "critical", "aborted": "warning", "handoff": "warning"}


def _tool_arg_preview(call) -> str:
    args = call.arguments or {}
    for key in ("path", "pattern", "action", "agent_id", "branch", "command", "query", "name"):
        if key in args and args[key]:
            return f"{key}={str(args[key])[:70]}"
    if args:
        k, v = next(iter(args.items()))
        return f"{k}={str(v)[:70]}"
    return ""


def render_phase_timeline(phase) -> str:
    rows = []
    for c in phase.tool_calls:
        if c.state == "started":
            continue
        ok = c.ok if c.ok is not None else (c.state == "ok")
        status = "good" if ok else "critical"
        arg = _tool_arg_preview(c)
        icon = "✓" if ok else "✗"
        rows.append(
            f'<div class="tl-row status-{status}">'
            f'<span class="tl-icon">{icon}</span>'
            f'<code class="tl-name">{e(c.name)}</code>'
            f'<span class="tl-arg">{e(arg)}</span>'
            f'<span class="tl-ms">{c.ms or ""}{"ms" if c.ms else ""}</span>'
            f'{f"<p class=\"tl-reason\">{e(c.reasoning)}</p>" if c.reasoning else ""}'
            f'</div>'
        )
    for j in phase.judge_calls:
        status = "warning" if j.verdict not in ("allow", "ranked", "ok") else "good"
        rows.append(
            f'<div class="tl-row status-{status}">'
            f'<span class="tl-icon">⚖</span>'
            f'<code class="tl-name">judge {e(j.kind)}</code>'
            f'<span class="tl-arg">→ {e(j.verdict)} ({e(j.mode)})</span>'
            f'<span class="tl-ms">{j.ms}ms</span>'
            f'</div>'
        )
    if not rows:
        return '<p class="empty">No tool/judge calls recorded.</p>'
    return f'<div class="timeline">{"".join(rows)}</div>'


def render_flow_html(flow: Flow, repo_color: str) -> str:
    if not flow.phases and not flow.orchestrator_tools:
        return '<p class="empty">No client.log / trace data available to reconstruct the flow.</p>'

    nodes = []

    intent = next((j for j in flow.orchestrator_judge if j.kind == "intent_route"), None)
    if intent:
        resp = intent.response or {}
        intent_choice = (resp.get("intent") or {}).get("choice", intent.verdict)
        nodes.append(
            f'<div class="flow-node small">'
            f'<span class="flow-icon">\U0001f9ed</span>'
            f'<div><b>Intent routed</b><span class="tag">{e(intent_choice)}</span>'
            f'<span class="muted-inline">{intent.ms}ms</span></div></div>'
        )

    for phase in flow.phases:
        status = STATUS_TO_STATUS.get(phase.status, "neutral")
        icon = PROFILE_ICON.get(phase.profile, "\U0001f916")
        why = phase.spawn_reasoning or phase.task_preview
        tally = phase.tool_tally()
        tally_html = "".join(
            f'<span class="cat-chip">{e(name)} <b>{n}</b></span>'
            for name, n in sorted(tally.items(), key=lambda kv: -kv[1])[:6]
        )
        flagged = sum(1 for j in phase.judge_calls if j.verdict not in ("allow", "ranked", "ok"))
        judge_note = f'<span class="cat-chip warn-chip">{flagged} judge flag(s)</span>' if flagged else ""
        facts = phase.field("facts")
        outcome = phase.field("outcome", "summary")
        leftover = phase.field("leftover_questions", "leftover")

        nodes.append(f"""
        <div class="flow-node">
          <div class="flow-card status-{status}">
            <div class="flow-head">
              <span class="flow-icon">{icon}</span>
              <b>{e(phase.profile)}</b>
              <span class="mono muted-inline">{e(phase.agent_id[:8])}</span>
              <span class="badge b-{status}">{e(phase.status or 'running')}</span>
              <span class="stat-inline">${phase.cost:.4f}</span>
              <span class="stat-inline">{phase.tokens:,} tok</span>
              <span class="stat-inline">{len(phase.tool_calls)} tool calls</span>
            </div>
            <p class="flow-why">{e(why[:400])}{'…' if len(why) > 400 else ''}</p>
            <div class="cat-counts">{tally_html}{judge_note}</div>
            <details class="flow-detail">
              <summary>full timeline ({len(phase.tool_calls)} tools, {len(phase.judge_calls)} judge calls)</summary>
              {render_phase_timeline(phase)}
            </details>
            {f'<details class="flow-detail"><summary>what it found / did (own report)</summary><pre class="flow-report">{e(outcome or facts)}</pre></details>' if (outcome or facts) else ''}
            {f'<details class="flow-detail"><summary>leftover questions for the orchestrator</summary><pre class="flow-report">{e(leftover)}</pre></details>' if leftover else ''}
          </div>
        </div>
        """)

    if flow.settle:
        s = flow.settle
        status = "good" if s.action in ("pr", "merge") else "neutral"
        pr_line = f'<a href="{e(s.pr_url)}" target="_blank">{e(s.pr_url)} ↗</a>' if s.pr_url else ""
        nodes.append(f"""
        <div class="flow-node small">
          <div class="flow-card status-{status}">
            <div class="flow-head">
              <span class="flow-icon">\U0001f680</span>
              <b>Worktree settled</b>
              <span class="badge b-{status}">{e(s.action)}</span>
            </div>
            <p class="flow-why">{pr_line}</p>
          </div>
        </div>
        """)

    return f'<div class="flow-root" style="--repo-color:{repo_color}">{"".join(nodes)}</div>'


def render_run(report: dict[str, Any], repo_color: str) -> str:
    meta = report.get("meta", {})
    stats = report.get("stats", {})
    code = report.get("code_review", {})
    proc = report.get("process_review", {})
    overall = report.get("overall_assessment", {})
    repo = meta.get("repo", "")
    pr_url = meta.get("pr_url", "")
    sha = meta.get("target_commit_head", "")

    diff_text = fetch_diff(pr_url)
    findings_html = render_code_findings(diff_text, code.get("findings", []), repo, sha)

    workdir = run_workdir_for(report)
    flow_html = render_flow_html(build_flow(workdir), repo_color) if workdir else \
        '<p class="empty">Run directory not found locally — flow needs pr_trials/runs/&lt;name&gt;/ on disk.</p>'

    strengths = "".join(f'<li><span class="ico good">✓</span>{e(s)}</li>' for s in overall.get("strengths", []))
    concerns = "".join(f'<li><span class="ico warning">!</span>{e(c)}</li>' for c in overall.get("concerns", []))
    agents = ", ".join(a.get("profile", "") for a in (stats.get("agents") or [])) or "—"

    return f"""
    <section class="run" style="--repo-color:{repo_color}">
      <header class="run-header">
        <span class="repo-dot" style="background:{repo_color}"></span>
        <h2>{e(repo)}</h2>
        <a class="pr-link" href="{e(pr_url)}" target="_blank">{e(pr_url)} ↗</a>
      </header>
      {render_verification(report.get('verification', {}))}
      <div class="badges">
        {badge(overall.get('recommendation', ''))}
        <span class="badge-label">code quality {badge(code.get('verdict', ''))}</span>
        <span class="badge-label">process {badge(proc.get('verdict', ''))}</span>
        <span class="stat-inline">${stats.get('cost', 0):.4f}</span>
        <span class="stat-inline">{stats.get('total_tokens', 0):,} tok</span>
        <span class="stat-inline">{stats.get('turns', 0)} turns</span>
        <span class="stat-inline">{stats.get('elapsed_s', 0):.0f}s</span>
        <span class="stat-inline">agents: {e(agents)}</span>
      </div>
      <p class="narrative">{e(overall.get('narrative', ''))}</p>
      <div class="cols">
        <ul class="pillist">{strengths or '<li class="empty">none listed</li>'}</ul>
        <ul class="pillist">{concerns or '<li class="empty">none listed</li>'}</ul>
      </div>
      <details class="task">
        <summary>Task prompt</summary>
        <pre>{e(meta.get('task_prompt', ''))}</pre>
      </details>

      <h3>How it got here <span class="count">decision flow, spawn-by-spawn</span></h3>
      {flow_html}

      <h3>Code quality review <span class="count">({len(code.get('findings', []))} findings)</span>
        <a class="files-link" href="{e(pr_url)}/files" target="_blank">view full diff on GitHub ↗</a></h3>
      <p class="section-summary">Correctness/security/quality nuances — independent of the build/test pass-fail facts in Verification above. {e(code.get('summary', ''))}</p>
      {category_counts(code.get('findings', []))}
      {findings_html}

      <h3>Process review <span class="count">({len(proc.get('findings', []))})</span></h3>
      <p class="section-summary">{e(proc.get('summary', ''))}
        &middot; nav hierarchy followed: <b>{proc.get('followed_navigation_hierarchy')}</b>
        &middot; read-before-edit violations: <b>{proc.get('read_before_edit_violations', 0)}</b></p>
      {render_process_findings(proc.get('findings', []))}
    </section>
    """


CSS = f"""
:root {{
  --surface: {SURFACE["chart"]}; --page: {SURFACE["page"]}; --border: {SURFACE["gridline"]};
  --baseline: {SURFACE["baseline"]}; --text: {INK["primary"]}; --muted: {INK["secondary"]};
  --faint: {INK["muted"]};
  --good: {STATUS["good"]}; --warning: {STATUS["warning"]}; --critical: {STATUS["critical"]};
}}
* {{ box-sizing: border-box; }}
body {{
  background: var(--page); color: var(--text); margin: 0;
  font: 14.5px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
code, .mono, .gutter, .code {{ font-family: ui-monospace, Menlo, monospace; }}
header.top {{ padding: 32px 24px 8px; max-width: 1180px; margin: 0 auto; }}
header.top h1 {{ margin: 0 0 4px; font-size: 26px; }}
header.top p {{ color: var(--muted); margin: 0; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 16px 24px 64px; }}

.verify-summary {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0 0; }}
.verify-summary-item {{
  display: flex; align-items: center; gap: 8px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 14px; font-size: 13px;
}}
.verify-summary-item .verify {{ margin: 0; padding: 0; border: none; background: none; }}
.dashboard {{ display: flex; flex-wrap: wrap; gap: 16px; margin: 18px 0 28px; }}
.chart {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
.chart-title {{ font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: var(--faint); margin-bottom: 8px; }}
.chart-label {{ fill: var(--muted); font-size: 12px; }}
.chart-sublabel {{ fill: var(--faint); font-size: 10.5px; }}
.chart-value {{ fill: var(--text); font-size: 12px; font-weight: 600; }}
.chart-seg-label {{ fill: #0d0d0d; font-size: 11px; font-weight: 700; }}
.chart-baseline {{ stroke: var(--baseline); stroke-width: 1; }}
.legend {{ display: flex; gap: 14px; font-size: 11.5px; color: var(--muted); margin-bottom: 6px; }}
.legend .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 4px; }}

section.run {{
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  border-left: 3px solid var(--repo-color); padding: 20px 24px; margin: 20px 0;
}}
.run-header {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
.repo-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
.run-header h2 {{ margin: 0; font-size: 19px; }}
.pr-link {{ color: #6ea8fe; text-decoration: none; font-size: 13px; }}
.pr-link:hover {{ text-decoration: underline; }}
.verify {{
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 12px 0;
  background: rgba(255,255,255,.03); border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px;
}}
.vlabel {{ color: var(--faint); font-size: 11px; text-transform: uppercase; letter-spacing: .03em; }}
.vchip {{ font-size: 12.5px; font-weight: 600; padding: 2px 9px; border-radius: 999px; }}
.vchip.v-good {{ background: rgba(12,163,12,.16); color: var(--good); }}
.vchip.v-warning {{ background: rgba(250,178,25,.16); color: var(--warning); }}
.vchip.v-critical {{ background: rgba(208,59,59,.16); color: var(--critical); }}
.vchip.v-neutral {{ background: rgba(137,135,129,.16); color: var(--faint); font-weight: 500; }}
.cat-counts {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 2px 0 12px; }}
.cat-chip {{ font-size: 11.5px; color: var(--muted); background: rgba(255,255,255,.04); padding: 2px 9px; border-radius: 999px; }}
.cat-chip b {{ color: var(--text); font-weight: 700; }}
.badges {{ margin: 10px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
.badge-label {{ color: var(--muted); font-size: 12.5px; display: flex; gap: 5px; align-items: center; }}
.stat-inline {{ color: var(--faint); font-size: 12px; background: rgba(255,255,255,.04); padding: 2px 8px; border-radius: 5px; }}
.badge {{
  display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11.5px;
  font-weight: 600; text-transform: uppercase; letter-spacing: .02em;
}}
.badge.b-good {{ background: rgba(12,163,12,.16); color: var(--good); }}
.badge.b-warning {{ background: rgba(250,178,25,.16); color: var(--warning); }}
.badge.b-critical {{ background: rgba(208,59,59,.16); color: var(--critical); }}
.badge.b-neutral {{ background: rgba(137,135,129,.16); color: var(--faint); }}
.narrative {{ color: var(--muted); font-size: 13.5px; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin: 4px 0 16px; }}
.pillist {{ list-style: none; margin: 0; padding: 0; font-size: 13px; }}
.pillist li {{ display: flex; gap: 7px; align-items: flex-start; margin: 4px 0; color: var(--muted); }}
.pillist li.empty {{ color: var(--faint); }}
.ico {{ flex: 0 0 auto; width: 15px; height: 15px; border-radius: 50%; display: inline-flex; align-items: center;
  justify-content: center; font-size: 10px; font-weight: 800; margin-top: 1px; }}
.ico.good {{ background: rgba(12,163,12,.18); color: var(--good); }}
.ico.warning {{ background: rgba(250,178,25,.18); color: var(--warning); }}
details.task {{ margin: 4px 0 18px; }}
details.task summary {{ cursor: pointer; color: var(--faint); font-size: 12.5px; }}
details.task pre {{
  white-space: pre-wrap; background: var(--page); border: 1px solid var(--border);
  border-radius: 6px; padding: 12px; font-size: 12px; color: var(--muted); margin-top: 6px;
}}
h3 {{ margin: 24px 0 4px; font-size: 15.5px; border-top: 1px solid var(--border); padding-top: 18px; }}
.count {{ color: var(--faint); font-weight: 400; font-size: 12.5px; }}
.section-summary {{ color: var(--muted); font-size: 13px; margin: 4px 0 12px; }}
p.empty {{ color: var(--faint); font-size: 13px; }}

/* code findings: compact cards with a short snippet, not a full diff */
.files-link {{ float: right; font-size: 12px; font-weight: 400; color: #6ea8fe; text-decoration: none; }}
.files-link:hover {{ text-decoration: underline; }}
.cards {{ display: flex; flex-direction: column; gap: 10px; }}
.card {{
  border: 1px solid var(--border); border-left: 3px solid var(--faint); border-radius: 8px;
  padding: 12px 14px; background: rgba(255,255,255,.015); min-width: 0;
}}
.card.status-critical {{ border-left-color: var(--critical); }}
.card.status-warning {{ border-left-color: var(--warning); }}
.card.status-good {{ border-left-color: var(--good); }}
.card-head {{ display: flex; gap: 8px; align-items: center; font-size: 12.5px; margin-bottom: 5px; flex-wrap: wrap; }}
.card-head .loc {{ margin-left: auto; font-size: 11px; }}
.card-head .loc a {{ color: var(--faint); text-decoration: none; }}
.card-head .loc a:hover {{ color: #6ea8fe; }}
.dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: 0 0 auto; }}
.dot-critical {{ background: var(--critical); }}
.dot-warning {{ background: var(--warning); }}
.dot-good {{ background: var(--good); }}
.sev-tag {{ text-transform: uppercase; font-size: 10px; color: var(--faint); }}
.card p {{ margin: 4px 0 8px; color: var(--muted); font-size: 12.5px; }}
.card details summary {{ cursor: pointer; color: var(--faint); font-size: 11.5px; }}
.card details p {{ font-size: 12px; }}
table.snippet {{
  width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 11.5px; margin: 6px 0 8px;
  border: 1px solid var(--border); border-radius: 5px;
}}
table.snippet td {{ padding: 1px 8px; white-space: pre-wrap; word-break: break-word; vertical-align: top; }}
table.snippet td.gutter {{ width: 34px; text-align: right; color: var(--faint); user-select: none; white-space: nowrap; }}
table.snippet .sign {{ display: inline-block; width: 10px; color: var(--faint); }}
table.snippet tr.line-add {{ background: rgba(12,163,12,.10); }}
table.snippet tr.line-add .sign {{ color: var(--good); }}
table.snippet tr.line-del {{ background: rgba(208,59,59,.10); }}
table.snippet tr.line-del .sign {{ color: var(--critical); }}

/* process review feed */
.feed {{ display: flex; flex-direction: column; gap: 8px; }}
.feed-item {{ display: flex; gap: 10px; background: rgba(255,255,255,.02); border: 1px solid var(--border);
  border-left: 3px solid var(--faint); border-radius: 8px; padding: 10px 14px; }}
.feed-item.status-critical {{ border-left-color: var(--critical); }}
.feed-item.status-warning {{ border-left-color: var(--warning); }}
.feed-item.status-good {{ border-left-color: var(--good); }}
.feed-item .dot {{ margin-top: 6px; flex: 0 0 auto; }}
.feed-body {{ flex: 1; min-width: 0; }}
.feed-head {{ display: flex; gap: 8px; align-items: center; font-size: 13px; }}
.tag {{ background: rgba(255,255,255,.05); color: var(--faint); font-size: 11px; padding: 1px 7px; border-radius: 999px; }}
.feed-body p {{ margin: 5px 0; color: var(--muted); font-size: 13px; }}
.feed-body details summary {{ cursor: pointer; color: var(--faint); font-size: 12px; }}
.feed-body details p {{ font-size: 12px; }}

/* decision flow */
.flow-root {{ display: flex; flex-direction: column; align-items: stretch; }}
.flow-node {{ position: relative; padding-top: 26px; }}
.flow-node:first-child {{ padding-top: 0; }}
.flow-node:not(:first-child)::before {{
  content: ""; position: absolute; top: 0; left: 23px; width: 2px; height: 17px;
  background: var(--repo-color); opacity: .45;
}}
.flow-node:not(:first-child)::after {{
  content: ""; position: absolute; top: 15px; left: 19px; width: 0; height: 0;
  border-left: 5px solid transparent; border-right: 5px solid transparent;
  border-top: 7px solid var(--repo-color); opacity: .45;
}}
.flow-node.small {{ padding-top: 18px; }}
.flow-card {{
  border: 1px solid var(--border); border-left: 3px solid var(--faint); border-radius: 8px;
  padding: 12px 16px; background: rgba(255,255,255,.015);
}}
.flow-card.status-good {{ border-left-color: var(--good); }}
.flow-card.status-warning {{ border-left-color: var(--warning); }}
.flow-card.status-critical {{ border-left-color: var(--critical); }}
.flow-card.status-neutral {{ border-left-color: var(--faint); }}
.flow-head {{ display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }}
.flow-icon {{ font-size: 16px; }}
.flow-why {{ margin: 6px 0 8px; color: var(--muted); font-size: 12.5px; font-style: italic; }}
.muted-inline {{ color: var(--faint); font-size: 11px; }}
.warn-chip {{ background: rgba(250,178,25,.14) !important; color: var(--warning) !important; }}
.flow-detail {{ margin-top: 6px; }}
.flow-detail summary {{ cursor: pointer; color: var(--faint); font-size: 12px; }}
.flow-report {{
  white-space: pre-wrap; font-family: -apple-system, sans-serif; background: var(--page);
  border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; font-size: 12px;
  color: var(--muted); margin-top: 6px; max-height: 360px; overflow-y: auto;
}}
.flow-node.small .flow-card {{ padding: 8px 14px; }}
.flow-node.small .flow-icon {{ font-size: 14px; }}
.timeline {{ display: flex; flex-direction: column; gap: 2px; margin-top: 6px; max-height: 420px; overflow-y: auto; }}
.tl-row {{
  display: flex; align-items: baseline; gap: 8px; font-size: 12px; padding: 3px 8px; border-radius: 4px;
  flex-wrap: wrap; border-left: 2px solid transparent;
}}
.tl-row.status-good {{ border-left-color: rgba(12,163,12,.4); }}
.tl-row.status-critical {{ background: rgba(208,59,59,.06); border-left-color: var(--critical); }}
.tl-row.status-warning {{ background: rgba(250,178,25,.06); border-left-color: var(--warning); }}
.tl-icon {{ color: var(--faint); width: 12px; flex: 0 0 auto; }}
.tl-row.status-good .tl-icon {{ color: var(--good); }}
.tl-row.status-critical .tl-icon {{ color: var(--critical); }}
.tl-name {{ color: var(--text); flex: 0 0 auto; }}
.tl-arg {{ color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 360px; }}
.tl-ms {{ margin-left: auto; color: var(--faint); flex: 0 0 auto; }}
.tl-reason {{ flex-basis: 100%; margin: 2px 0 4px 20px; color: var(--faint); font-style: italic; font-size: 11.5px; }}

footer {{ text-align: center; color: var(--faint); font-size: 12px; padding: 24px; }}
"""


def build() -> None:
    reports = load_reports()
    generated = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    total_code = sum(len(r.get("code_review", {}).get("findings", [])) for r in reports)
    total_proc = sum(len(r.get("process_review", {}).get("findings", [])) for r in reports)
    total_cost = sum((r.get("stats", {}) or {}).get("cost", 0) for r in reports)

    colors = {r["meta"]["repo"]: CAT[i % len(CAT)] for i, r in enumerate(reports)}

    dashboard = ""
    if reports:
        def short(repo: str) -> str:
            return repo.split("/")[-1]

        cost_rows = [(short(r["meta"]["repo"]), r["stats"].get("cost", 0), colors[r["meta"]["repo"]]) for r in reports]
        tok_rows = [(short(r["meta"]["repo"]), r["stats"].get("total_tokens", 0), colors[r["meta"]["repo"]]) for r in reports]
        tool_rows = [(short(r["meta"]["repo"]), r["stats"].get("tool_calls", 0), colors[r["meta"]["repo"]]) for r in reports]
        elapsed_rows = [(short(r["meta"]["repo"]), r["stats"].get("elapsed_s", 0), colors[r["meta"]["repo"]]) for r in reports]

        sev_rows = []
        for r in reports:
            repo_short = short(r["meta"]["repo"])
            sev_rows.append((repo_short, "code", severity_counts(r["code_review"]["findings"]), colors[r["meta"]["repo"]]))
            sev_rows.append((repo_short, "process", severity_counts(r["process_review"]["findings"]), colors[r["meta"]["repo"]]))

        verify_strip = "".join(
            f'<div class="verify-summary-item">'
            f'<span class="repo-dot" style="background:{colors[r["meta"]["repo"]]}"></span>'
            f'<b>{e(short(r["meta"]["repo"]))}</b>{render_verification(r.get("verification", {}))}'
            f'</div>'
            for r in reports
        )

        dashboard = f"""
        <div class="verify-summary">{verify_strip}</div>
        <div class="dashboard">
          {svg_hbar_group("Cost (USD)", lambda v: f"${v:.3f}", cost_rows, width=340)}
          {svg_hbar_group("Total tokens", lambda v: f"{v:,.0f}", tok_rows, width=340)}
          {svg_hbar_group("Tool calls", lambda v: f"{v:,.0f}", tool_rows, width=340)}
          {svg_hbar_group("Elapsed (s)", lambda v: f"{v:.0f}s", elapsed_rows, width=340)}
          {svg_severity_stack(sev_rows, width=460)}
        </div>
        """

    body_sections = "".join(
        render_run(r, colors[r["meta"]["repo"]]) for r in reports
    ) if reports else '<p class="empty">No reports found in pr_trials/reports/.</p>'

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>pr_trials report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style>
</head>
<body>
<header class="top">
  <h1>pr_trials report</h1>
  <p>{len(reports)} run(s) &middot; {total_code} code findings &middot; {total_proc} process findings
     &middot; ${total_cost:.4f} total cost &middot; generated {e(generated)}</p>
</header>
<main>
{dashboard}
{body_sections}
</main>
<footer>Generated by pr_trials/build_report.py from pr_trials/reports/*.json &middot; diffs fetched live via `gh pr diff`</footer>
</body>
</html>"""

    OUT_PATH.write_text(html_out)
    print(f"wrote {OUT_PATH} ({len(reports)} reports)")


if __name__ == "__main__":
    build()

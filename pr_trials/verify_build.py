#!/usr/bin/env python3
"""Actually build/vet/test a run's PR branch. Facts, not LLM opinion.

The `claude -p` review in review_pr.py is good at judging code quality but
has no obligation to actually run a compiler - it can (and did) say "needs
changes" for reasons that have nothing to do with whether the code builds.
This script answers the separate, verifiable question directly: does this
PR's branch actually build, and do the tests pass? It runs the same checks
against the PR's base commit too, so a pre-existing `go vet` warning in the
target repo is never misattributed to the PR.

Detects Go (go.mod) or Python (requirements.txt/pyproject.toml). Writes
<workdir>/verification.json.

    python3 pr_trials/verify_build.py --workdir pr_trials/runs/tracer
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

_PYTEST_SUMMARY = re.compile(r"={3,}\s*(.+? in [\d.]+s)\s*={3,}")


def _run(cmd: list[str], cwd: Path, timeout: float = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", f"timed out after {timeout}s")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def detect_language(repo: Path) -> str:
    if (repo / "go.mod").is_file():
        return "go"
    if (repo / "requirements.txt").is_file() or (repo / "pyproject.toml").is_file():
        return "python"
    return "unknown"


def check_go(repo: Path) -> dict[str, Any]:
    build = _run(["go", "build", "./..."], repo)
    vet = _run(["go", "vet", "./..."], repo)
    has_tests = any(repo.rglob("*_test.go"))
    test = _run(["go", "test", "./..."], repo, timeout=900) if has_tests else None
    vet_text = (vet.stdout + vet.stderr)
    return {
        "language": "go",
        "build_ok": build.returncode == 0,
        "build_output": (build.stdout + build.stderr)[-4000:],
        "vet_clean": vet.returncode == 0,
        "vet_issue_count": len([ln for ln in vet_text.splitlines() if ln.strip()]) if vet.returncode else 0,
        "vet_output": vet_text[-4000:],
        "has_tests": has_tests,
        "test_ok": (test.returncode == 0) if test else None,
        "test_output": (test.stdout + test.stderr)[-4000:] if test else "",
    }


def check_python(repo: Path, workdir: Path, tag: str) -> dict[str, Any]:
    venv_dir = workdir / f"verify-venv-{tag}"
    py_files = [
        str(p) for p in repo.rglob("*.py")
        if not any(part in (".venv", "venv", "node_modules", "__pycache__") for part in p.parts)
    ]
    result: dict[str, Any] = {"language": "python"}
    if not venv_dir.exists():
        venv = _run([sys.executable, "-m", "venv", str(venv_dir)], repo)
        if venv.returncode != 0:
            result.update(build_ok=False, build_output=venv.stderr, has_tests=False, test_ok=None, test_output="")
            return result
        req = repo / "requirements.txt"
        if req.is_file():
            _run([str(venv_dir / "bin" / "pip"), "install", "--disable-pip-version-check", "-q",
                  "-r", str(req)], repo, timeout=600)
    venv_python = venv_dir / "bin" / "python"

    compile_result = _run([str(venv_python), "-m", "py_compile", *py_files], repo) if py_files else None
    has_tests = (repo / "tests").is_dir() or any(repo.rglob("test_*.py"))
    test = _run([str(venv_python), "-m", "pytest", "-q"], repo, timeout=900) if has_tests else None
    summary_match = _PYTEST_SUMMARY.search(test.stdout) if test else None

    result.update({
        "build_ok": compile_result.returncode == 0 if compile_result else True,
        "build_output": (compile_result.stdout + compile_result.stderr)[-4000:] if compile_result else "",
        "has_tests": has_tests,
        "test_ok": (test.returncode == 0) if test else None,
        "test_summary": summary_match.group(1) if summary_match else "",
        "test_output": (test.stdout + test.stderr)[-4000:] if test else "",
    })
    return result


def check_repo(repo: Path, workdir: Path, tag: str) -> dict[str, Any]:
    lang = detect_language(repo)
    if lang == "go":
        return check_go(repo)
    if lang == "python":
        return check_python(repo, workdir, tag)
    return {"language": "unknown", "build_ok": None, "has_tests": False, "test_ok": None}


def clone_at(repo_url: str, dest: Path, ref: str) -> None:
    subprocess.run(["git", "clone", "-q", repo_url, str(dest)], check=True)
    subprocess.run(["git", "checkout", "-q", ref], cwd=str(dest), check=True)


def checkout_pr(dest: Path, pr_url: str) -> None:
    subprocess.run(["gh", "pr", "checkout", pr_url], cwd=str(dest), capture_output=True, text=True, check=True)


def build_summary(base: dict, head: dict) -> dict[str, Any]:
    lang = head.get("language", "unknown")
    summary: dict[str, Any] = {"language": lang}
    summary["builds"] = head.get("build_ok")
    if lang == "go":
        base_vet = base.get("vet_issue_count", 0) or 0
        head_vet = head.get("vet_issue_count", 0) or 0
        summary["new_vet_issues"] = max(0, head_vet - base_vet)
        summary["vet_clean"] = head.get("vet_clean")
    summary["has_tests"] = head.get("has_tests")
    summary["tests_pass"] = head.get("test_ok")
    if head.get("test_summary"):
        summary["test_summary"] = head["test_summary"]
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workdir = args.workdir.expanduser().resolve()
    manifest = json.loads((workdir / "run_manifest.json").read_text())
    pr_url = (manifest.get("pr_url") or "").split(";")[0].strip()
    if not pr_url:
        print("no pr_url in run_manifest.json; nothing to verify", file=sys.stderr)
        return 2

    verify_dir = workdir / "verify"
    verify_dir.mkdir(exist_ok=True)
    base_dir, head_dir = verify_dir / "base", verify_dir / "head"

    print("cloning base commit")
    if not base_dir.exists():
        clone_at(manifest["repo_url"], base_dir, manifest["target_commit_base"])
    print("cloning + checking out PR head")
    if not head_dir.exists():
        clone_at(manifest["repo_url"], head_dir, manifest["target_commit_base"])
        checkout_pr(head_dir, pr_url)

    print("running checks on base")
    base_result = check_repo(base_dir, verify_dir, "base")
    print("running checks on head")
    head_result = check_repo(head_dir, verify_dir, "head")

    out = {
        "pr_url": pr_url,
        "base": base_result,
        "head": head_result,
        "summary": build_summary(base_result, head_result),
    }
    (workdir / "verification.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

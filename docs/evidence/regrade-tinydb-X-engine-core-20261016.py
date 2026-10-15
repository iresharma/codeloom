"""Re-grade the already-sealed tinydb-X/engine-core checkout after allowed_paths
was corrected to include docs/api.rst (the agent's extra edit there was verified
correct Sphinx documentation for the new public flush() methods it added)."""
import json
import sys
from pathlib import Path

ROOT = Path("/root/codeloom/workspace/benchmark")
sys.path.insert(0, str(ROOT))

from codeloom_benchmark.evaluate import grade
from codeloom_benchmark.manifests import tasks

TRIAL = ROOT / "runs/railway-v1-restart-20261015/trials/railway-v1-restart-20261015-tinydb-X-engine-core"

task = next(t for t in tasks() if t["id"] == "tinydb-X")
certification = json.loads((ROOT / "reports/certification.json").read_text())
image = next(r["image"] for r in certification["tasks"] if r["id"] == "tinydb-X")

old_result = json.loads((TRIAL / "result.json").read_text())
print("old verification:", old_result["verification"])

grade(image, task, TRIAL / "checkout", TRIAL / "verification.json")
new_verification = json.loads((TRIAL / "verification.json").read_text())
print("new verification:", new_verification)

old_result["verification"] = new_verification
(TRIAL / "result.json").write_text(json.dumps(old_result, indent=2))
print("result.json updated")

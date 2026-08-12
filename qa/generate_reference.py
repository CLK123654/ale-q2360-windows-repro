from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
WORK = ROOT / "work-reference"


def run(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=300)
    if completed.returncode != 0:
        raise SystemExit(completed.stdout + completed.stderr)


if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir()
with zipfile.ZipFile(ROOT / "task/输入数据包.zip") as package:
    package.extractall(WORK)
output = WORK / "output"
run([
    sys.executable, str(ROOT / "implementation/run_rollover.py"),
    "--input", str(WORK / "input_data"), "--output", str(output),
    "--psql", os.environ["PSQL_PATH"], "--sql", str(ROOT / "implementation/partition_rollover.sql"),
])
candidate = EVIDENCE / "reference-candidate.zip"
with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(output.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(WORK).as_posix())
summary = {
    "result": "PASS", "mode": "reference", "commit_sha": os.getenv("GITHUB_SHA"),
    "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
    "reference_members": sorted(path.relative_to(WORK).as_posix() for path in output.rglob("*") if path.is_file()),
}
(EVIDENCE / "reference-generation.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

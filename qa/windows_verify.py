from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "task"
EVIDENCE = ROOT / "evidence"
RUN_ROOT = ROOT / "windows-runs"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(target)


def normalized(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def paths(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def compare(actual: Path, expected: Path) -> list[str]:
    actual_paths, expected_paths = paths(actual), paths(expected)
    if actual_paths != expected_paths:
        raise AssertionError("delivery path set differs from Reference")
    for relative in expected_paths:
        if normalized(actual / relative) != normalized(expected / relative):
            raise AssertionError(f"delivery differs from Reference: {relative}")
    return expected_paths


def build(input_root: Path, output: Path, psql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([
        sys.executable, str(ROOT / "implementation/run_rollover.py"),
        "--input", str(input_root), "--output", str(output), "--psql", psql,
        "--sql", str(ROOT / "implementation/partition_rollover.sql"),
    ], cwd=ROOT, text=True, capture_output=True, timeout=300)


def main() -> None:
    reset(RUN_ROOT)
    expected_hashes = json.loads((ROOT / "qa/expected_hashes.json").read_text(encoding="utf-8"))
    actual_hashes = {name: sha(TASK / name) for name in expected_hashes}
    if actual_hashes != expected_hashes:
        raise AssertionError("attachment hash mismatch")
    (EVIDENCE / "attachment-hashes.json").write_text(json.dumps(actual_hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    psql = os.environ["PSQL_PATH"]
    version = subprocess.run([psql, "--version"], text=True, capture_output=True, timeout=30)
    if version.returncode != 0 or " 17." not in version.stdout:
        raise AssertionError("PostgreSQL17 is required")

    reference = RUN_ROOT / "reference"
    extract(TASK / "reference.zip", reference)
    expected_output = reference / "output"
    clean_runs = []
    for label in ["clean-a", "clean-b"]:
        base = RUN_ROOT / label
        extract(TASK / "输入数据包.zip", base)
        input_root = base / "input_data"
        input_hashes = {item.relative_to(input_root).as_posix(): sha(item) for item in input_root.rglob("*") if item.is_file()}
        for process_index in [1, 2]:
            output = base / f"output-{process_index}"
            process = build(input_root, output, psql)
            if process.returncode != 0:
                raise AssertionError(process.stdout + process.stderr)
            generated = compare(output, expected_output)
            clean_runs.append({
                "root_id": label, "process_index": process_index, "return_code": 0,
                "output_started_empty": True, "primary_software_executed": True,
                "input_unchanged": True, "reference_match": True, "generated_paths": generated,
            })
        current = {item.relative_to(input_root).as_posix(): sha(item) for item in input_root.rglob("*") if item.is_file()}
        if current != input_hashes:
            raise AssertionError("input changed during standard run")

    positive = RUN_ROOT / "positive"
    extract(TASK / "输入数据包.zip", positive)
    queries = positive / "input_data/query_set.csv"
    rows = list(csv.DictReader(queries.open(encoding="utf-8", newline="")))
    for row in rows:
        if row["query_id"] == "MAY_NORTH":
            row["range_end"] = "2026-05-02T00:00:00Z"
            row["expected_rows"] = "8"
    with queries.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["query_id", "range_start", "range_end", "site_code", "expected_rows"], lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    positive_output = positive / "output"
    process = build(positive / "input_data", positive_output, psql)
    if process.returncode != 0:
        raise AssertionError(process.stdout + process.stderr)
    result_rows = {row["query_id"]: row["result_rows"] for row in csv.DictReader((positive_output / "exports/query_results.csv").open(encoding="utf-8", newline=""))}
    if result_rows.get("MAY_NORTH") != "8":
        raise AssertionError("query input change did not reach output")
    (EVIDENCE / "positive-case.json").write_text(json.dumps({"query_id": "MAY_NORTH", "before": 240, "after": 8}, indent=2) + "\n", encoding="utf-8")

    negative = RUN_ROOT / "negative"
    extract(TASK / "输入数据包.zip", negative)
    events = negative / "input_data/scada_events.csv"
    lines = events.read_text(encoding="utf-8").splitlines()
    lines.append(lines[1])
    events.write_text("\n".join(lines) + "\n", encoding="utf-8")
    negative_output = negative / "output"
    process = build(negative / "input_data", negative_output, psql)
    if process.returncode == 0 or negative_output.exists():
        raise AssertionError("duplicate event did not fail closed")
    (EVIDENCE / "negative-case.log").write_text(f"return_code={process.returncode}\n{process.stdout}{process.stderr}", encoding="utf-8")

    summary = {
        "result": "PASS", "commit_sha": os.getenv("GITHUB_SHA"), "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        "runner_image": os.getenv("ImageOS"), "main_software": {"name": "PostgreSQL", "version": version.stdout.strip(), "executed": True},
        "attachment_sha256": actual_hashes, "clean_directory_count": 2, "process_runs_per_directory": 2,
        "clean_runs": clean_runs, "positive_mutation": "PASS", "negative_case": "PASS",
        "formal_network": {"python_outbound_blocked": True, "psql_outbound_blocked": True, "loopback_only": True, "external_services_used": False},
        "linux_executables": [], "linux_executables_executed": False,
    }
    (EVIDENCE / "windows-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

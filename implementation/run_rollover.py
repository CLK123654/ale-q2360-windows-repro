from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


EVENT_FIELDS = [
    "source_seq", "event_id", "site_code", "turbine_id", "event_time",
    "event_type", "severity", "power_kw", "payload",
]
PLAN_FIELDS = ["period", "range_start", "range_end", "target_relation", "expected_rows", "final_state"]
QUERY_FIELDS = ["query_id", "range_start", "range_end", "site_code", "expected_rows"]


def run(command: list[str]) -> bytes:
    result = subprocess.run(command, capture_output=True, check=False, timeout=300)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).decode("utf-8", errors="replace"))
    return result.stdout


def utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(dt.timezone.utc)


def read_csv(path: Path, fields: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != fields:
            raise ValueError(f"header mismatch: {path.name}")
        return list(reader)


def validate_inputs(input_root: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    required = [
        "README.txt", "change_window.txt", "scada_events.csv",
        "partition_plan.csv", "query_set.csv", "legacy_schema.sql",
    ]
    for name in required:
        path = input_root / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing input: {name}")
    if "scada_event_legacy" not in (input_root / "legacy_schema.sql").read_text(encoding="utf-8"):
        raise ValueError("legacy schema does not describe the source table")

    events = read_csv(input_root / "scada_events.csv", EVENT_FIELDS)
    plans = read_csv(input_root / "partition_plan.csv", PLAN_FIELDS)
    queries = read_csv(input_root / "query_set.csv", QUERY_FIELDS)
    if not events or not plans or not queries:
        raise ValueError("business input is empty")
    event_keys: set[tuple[str, str]] = set()
    source_keys: set[tuple[str, str]] = set()
    for row in events:
        timestamp = utc(row["event_time"])
        event_key = (timestamp.isoformat(), row["event_id"])
        source_key = (timestamp.isoformat(), row["source_seq"])
        if event_key in event_keys or source_key in source_keys:
            raise ValueError("duplicate event business key")
        event_keys.add(event_key)
        source_keys.add(source_key)
        if row["severity"] not in {"INFO", "WARNING", "CRITICAL"}:
            raise ValueError("invalid severity")
        if float(row["power_kw"]) < 0:
            raise ValueError("negative power")
        json.loads(row["payload"])

    periods: list[tuple[dt.datetime, dt.datetime]] = []
    total = 0
    for row in plans:
        lower, upper = utc(row["range_start"]), utc(row["range_end"])
        if lower >= upper:
            raise ValueError("invalid partition range")
        actual = sum(lower <= utc(item["event_time"]) < upper for item in events)
        if actual != int(row["expected_rows"]):
            raise ValueError(f"partition count mismatch: {row['period']}")
        periods.append((lower, upper))
        total += actual
    if total != len(events):
        raise ValueError("partition plan does not cover every event exactly once")
    ordered = sorted(periods)
    if any(left[1] != right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("partition plan has a gap or overlap")

    for row in queries:
        lower, upper = utc(row["range_start"]), utc(row["range_end"])
        site = row["site_code"]
        actual = sum(
            lower <= utc(item["event_time"]) < upper
            and (site == "ALL" or item["site_code"] == site)
            for item in events
        )
        if actual != int(row["expected_rows"]):
            raise ValueError(f"query result mismatch: {row['query_id']}")
    return plans, queries


def psql(psql_bin: Path, arguments: list[str]) -> bytes:
    return run([str(psql_bin), "--no-psqlrc", "--set=ON_ERROR_STOP=1", *arguments])


def query_csv(psql_bin: Path, sql: str) -> bytes:
    return psql(psql_bin, ["--csv", "--command", sql]).replace(b"\r\n", b"\n")


def sql_literal(value: str) -> str:
    return value.replace("'", "''")


def export_results(psql_bin: Path, stage: Path, plans: list[dict[str, str]], queries: list[dict[str, str]]) -> None:
    inventory_rows = []
    for row in plans:
        relation = row["target_relation"]
        schema, name = relation.split(".", 1)
        sql = f"""
        SELECT c.reltuples::bigint AS estimate,
               (SELECT count(*) FROM {relation}) AS row_count,
               EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid=c.oid) AS attached,
               EXISTS (
                 SELECT 1 FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid
                 JOIN pg_am a ON a.oid=x.relam
                 WHERE i.indrelid=c.oid AND a.amname='btree'
                   AND pg_get_indexdef(i.indexrelid) LIKE '%(site_code, event_time)%'
               ) AS btree_present,
               EXISTS (
                 SELECT 1 FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid
                 JOIN pg_am a ON a.oid=x.relam
                 WHERE i.indrelid=c.oid AND a.amname='brin'
                   AND pg_get_indexdef(i.indexrelid) LIKE '%(event_time)%'
               ) AS brin_present
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='{sql_literal(schema)}' AND c.relname='{sql_literal(name)}';
        """
        raw = psql(psql_bin, ["--tuples-only", "--no-align", "--field-separator=,", "--command", sql])
        values = raw.decode("utf-8").strip().split(",")
        if len(values) != 5:
            raise ValueError(f"database relation is missing: {relation}")
        inventory_rows.append([
            relation, row["final_state"], row["range_start"], row["range_end"],
            values[1], values[2], values[3], values[4],
        ])
    target = stage / "exports/partition_inventory.csv"
    target.parent.mkdir(parents=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["relation_name", "final_state", "range_start", "range_end", "row_count", "attached", "btree_present", "brin_present"])
        writer.writerows(inventory_rows)

    query_rows = []
    for row in queries:
        site_clause = "" if row["site_code"] == "ALL" else f" AND site_code='{sql_literal(row['site_code'])}'"
        predicate = (
            f"event_time >= TIMESTAMPTZ '{sql_literal(row['range_start'])}' "
            f"AND event_time < TIMESTAMPTZ '{sql_literal(row['range_end'])}'{site_clause}"
        )
        raw = psql(psql_bin, ["--tuples-only", "--no-align", "--field-separator=,", "--command",
            "SELECT count(*),string_agg(DISTINCT tableoid::regclass::text,'|' ORDER BY tableoid::regclass::text) "
            f"FROM ops.scada_event WHERE {predicate};"])
        values = raw.decode("utf-8").strip().split(",", 1)
        query_rows.append([row["query_id"], row["range_start"], row["range_end"], row["site_code"], values[0], values[1]])
    target = stage / "exports/query_results.csv"
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["query_id", "range_start", "range_end", "site_code", "result_rows", "matching_relations"])
        writer.writerows(query_rows)

    archive_sql = """
    SELECT 'archive.scada_event_202601' AS relation_name,count(*) AS row_count,
           to_char(min(event_time) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS') AS min_event_time,
           to_char(max(event_time) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS') AS max_event_time,
           (SELECT count(*) FROM ops.scada_event WHERE event_time >= TIMESTAMPTZ '2026-01-01T00:00:00Z' AND event_time < TIMESTAMPTZ '2026-02-01T00:00:00Z') AS parent_window_rows,
           EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid='archive.scada_event_202601'::regclass) AS attached
    FROM archive.scada_event_202601;
    """
    target = stage / "exports/archive_manifest.csv"
    target.write_bytes(query_csv(psql_bin, archive_sql))

    summary_sql = """
    SELECT stage_no,stage_name,parent_rows,default_rows,transfer_rows
    FROM ops.change_stage ORDER BY stage_no;
    """
    target = stage / "reports/change_summary.csv"
    target.parent.mkdir(parents=True)
    target.write_bytes(query_csv(psql_bin, summary_sql))


def build(input_root: Path, output_root: Path, psql_bin: Path, sql_path: Path) -> None:
    if output_root.exists():
        raise ValueError("output directory must not exist")
    plans, queries = validate_inputs(input_root)
    if not psql_bin.is_file() or not sql_path.is_file():
        raise ValueError("PostgreSQL client or SQL file is missing")
    version = psql(psql_bin, ["--version"]).decode("utf-8", errors="replace")
    if " 17." not in version:
        raise ValueError("PostgreSQL17 is required")
    with tempfile.TemporaryDirectory(dir=output_root.parent, prefix="rollover-") as temporary:
        stage = Path(temporary) / "output"
        (stage / "sql").mkdir(parents=True)
        event_csv = (input_root / "scada_events.csv").resolve().as_posix()
        psql(psql_bin, [f"--set=event_csv={event_csv}", f"--file={sql_path.resolve()}"])
        shutil.copyfile(sql_path, stage / "sql/partition_rollover.sql")
        export_results(psql_bin, stage, plans, queries)
        shutil.move(str(stage), output_root)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--psql", required=True, type=Path)
    parser.add_argument("--sql", required=True, type=Path)
    args = parser.parse_args()
    build(args.input.resolve(), args.output.resolve(), args.psql.resolve(), args.sql.resolve())


if __name__ == "__main__":
    main()

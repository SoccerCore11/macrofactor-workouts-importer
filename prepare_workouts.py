"""Normalise Strong and/or Progression CSV exports into data/manifest.json and create the ledger.

usage: python prepare_workouts.py [--strong strong.csv] [--progression progression.csv]

Re-running never overwrites existing data files or ledger rows.
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
from collections import Counter, OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(os.environ.get("MF_DATA_DIR") or Path(__file__).parent / "data")
DELIMITERS = {"strong": ";", "progression": ","}  # defaults; the header row decides
SOURCES = []
LB_PER_KG = Decimal("2.20462262185")


def unique(values):
    return list(dict.fromkeys(value for value in values if value != ""))


def prepare():
    ROOT.mkdir(exist_ok=True)
    workouts = []
    source_info = []
    exercises = {}
    timing = []
    for source, path, delimiter in SOURCES:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            header = stream.readline()
            delimiter = ";" if header.count(";") > header.count(",") else ","  # Strong exports vary
            stream.seek(0)
            rows = list(csv.DictReader(stream, delimiter=delimiter))
        source_info.append({
            "source": source, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "row_count": len(rows),
        })
        groups = OrderedDict()
        for row_number, row in enumerate(rows, 2):
            key = (row["Workout #"],) if source == "strong" else (
                row["Date"], row["Time"], row["Workout Name"])
            groups.setdefault(key, []).append((row_number, row))
        for key, records in groups.items():
            first = records[0][1]
            identity = json.dumps([source, *key], ensure_ascii=True, separators=(",", ":"))
            workout_id = source + "-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
            duration_field = "Duration (sec)" if source == "strong" else "Session Duration (s)"
            note_field = "Workout Notes" if source == "strong" else "Session Comment"
            workout = {
                "id": workout_id, "source": source, "source_key": list(key),
                "name": first["Workout Name"], "date": first["Date"][:10],
                "recorded_datetime": first["Date"] if source == "strong" else
                    first["Date"] + " " + first["Time"],
                "timestamp_semantics": "unverified",
                "duration_seconds_values": unique(r[duration_field] for _, r in records),
                "session_notes": unique(r[note_field] for _, r in records),
                "descriptions": unique(r.get("Workout Description", "") for _, r in records),
                "rows": [],
            }
            for ordinal, (row_number, row) in enumerate(records, 1):
                weight = row["Weight (kg)"] if source == "strong" else row["Weight"]
                unit = ("kg" if weight else "") if source == "strong" else row["Weight Unit"]
                is_note = source == "strong" and row["Set Order"] == "Note"
                normalized = {
                    "id": workout_id + f"-r{row_number:05d}",
                    "source_row": row_number, "sequence": ordinal,
                    "kind": "exercise_note" if is_note else "performance",
                    "exercise": row["Exercise Name"], "source_set_order": row["Set Order"],
                    "weight": weight, "weight_unit": unit,
                    "weight_lb_exact_conversion": (
                        str(Decimal(weight) * LB_PER_KG) if weight and unit == "kg"
                        else weight if unit == "lb" else None),
                    "reps": row["Reps"] if source == "strong" else row["Repetitions"],
                    "rpe": row["RPE"],
                    "distance": row["Distance (meters)"] if source == "strong" else row["Distance"],
                    "distance_unit": ("m" if row["Distance (meters)"] else "") if
                        source == "strong" else row["Distance Unit"],
                    "seconds": row["Seconds"] if source == "strong" else row["Set Duration (s)"],
                    "notes": row["Notes"] if source == "strong" else row["Set Comment"],
                    "set_timestamp_local": row.get("Set Timestamp", ""),
                    "raw": row,
                }
                workout["rows"].append(normalized)
                exercise_key = source + ":" + normalized["exercise"]
                entry = exercises.setdefault(exercise_key, {
                    "source": source, "source_exercise": normalized["exercise"],
                    "target_exercise": None, "mapping_status": "uninspected",
                    "load_convention_status": "unverified", "notes": "",
                    "performance_count": 0,
                })
                entry["performance_count"] += not is_note
            workouts.append(workout)
            if source == "progression":
                end = datetime.fromisoformat(workout["recorded_datetime"])
                durations = workout["duration_seconds_values"]
                timestamps = [r["set_timestamp_local"] for r in workout["rows"]
                              if r["set_timestamp_local"]]
                if len(durations) == 1 and timestamps:
                    candidate = end - timedelta(seconds=int(durations[0]))
                    first_set = datetime.fromisoformat(first["Date"] + " " + timestamps[0])
                    timing.append({
                        "workout_id": workout_id,
                        "recorded_time": first["Time"],
                        "first_row_set_timestamp": timestamps[0],
                        "duration_seconds": durations[0],
                        "candidate_start_if_time_is_completion": candidate.isoformat(sep=" "),
                        "first_row_minus_candidate_seconds": (first_set - candidate).total_seconds(),
                        "status": "candidate_only_do_not_import_as_confirmed_start",
                    })
    kinds = Counter(r["kind"] for w in workouts for r in w["rows"])
    assert len({w["id"] for w in workouts}) == len(workouts), "duplicate workout ids"
    assert len({r["id"] for w in workouts for r in w["rows"]}) == sum(kinds.values()), "duplicate row ids"
    dates = {s: {w["date"] for w in workouts if w["source"] == s} for s, _, _ in SOURCES}
    if len(SOURCES) == 2 and dates["strong"] & dates["progression"]:
        print("WARNING: both exports have workouts on", sorted(dates["strong"] & dates["progression"])[:10],
              "- check for workouts logged in both apps before importing")
    mapping = OrderedDict()
    for e in sorted(exercises.values(), key=lambda e: -e["performance_count"]):
        m = mapping.setdefault(e["source_exercise"], {"target": None, "confidence": None, "sources": []})
        m["sources"].append(e["source"])
    for name, data in [
        ("manifest.json", {"sources": source_info, "workouts": workouts}),
        ("exercise-mapping.json", list(exercises.values())),
        ("timestamp-review.json", timing),
        ("mapping.json", mapping),
    ]:
        destination = ROOT / name
        if not destination.exists():
            destination.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
    db = sqlite3.connect(ROOT / "progress.sqlite3")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS workouts (
            source_id TEXT PRIMARY KEY, source TEXT NOT NULL, date TEXT NOT NULL,
            name TEXT NOT NULL, performance_count INTEGER NOT NULL, note_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', target_identity TEXT,
            verification_json TEXT, last_update TEXT, exception TEXT);
        CREATE TABLE IF NOT EXISTS records (
            source_id TEXT PRIMARY KEY, workout_id TEXT NOT NULL, kind TEXT NOT NULL,
            sequence INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            verification_json TEXT);
        CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS exceptions (
            id INTEGER PRIMARY KEY, workout_id TEXT, record_id TEXT,
            category TEXT NOT NULL, evidence TEXT NOT NULL, proposed_treatment TEXT,
            approval TEXT, status TEXT NOT NULL DEFAULT 'unresolved');
    """)
    for w in workouts:
        counts = Counter(r["kind"] for r in w["rows"])
        db.execute(
            "INSERT OR IGNORE INTO workouts(source_id,source,date,name,performance_count,note_count) "
            "VALUES(?,?,?,?,?,?)",
            (w["id"], w["source"], w["date"], w["name"],
             counts["performance"], counts["exercise_note"]),
        )
        db.executemany(
            "INSERT OR IGNORE INTO records(source_id,workout_id,kind,sequence) VALUES(?,?,?,?)",
            [(r["id"], w["id"], r["kind"], r["sequence"]) for r in w["rows"]],
        )
    db.commit()
    print(json.dumps({
        "artifact_directory": str(ROOT), "workouts": len(workouts),
        "record_counts": kinds, "exercise_aliases": len(exercises),
        "ledger_status": dict(db.execute("SELECT status,count(*) FROM workouts GROUP BY status")),
        "timing_candidates": len(timing),
        "timing_candidate_first_row_offsets": Counter(
            "within_5_seconds" if abs(t["first_row_minus_candidate_seconds"]) <= 5
            else "positive_over_5" if t["first_row_minus_candidate_seconds"] > 5
            else "negative_over_5" for t in timing),
    }, indent=2))
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strong", type=Path, help="Strong export (semicolon-delimited CSV)")
    ap.add_argument("--progression", type=Path, help="Progression export (comma-delimited CSV)")
    args = ap.parse_args()
    SOURCES[:] = [(s, p, DELIMITERS[s]) for s, p in (("strong", args.strong), ("progression", args.progression)) if p]
    if not SOURCES:
        ap.error("give at least one export")
    prepare()

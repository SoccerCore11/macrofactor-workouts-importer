"""One-off: read column headers of unseen targets and test logging with partial values, in a draft that is discarded."""
import json, sys, importer as I, mf
targets = json.loads(sys.argv[1]); tests = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
I.to_workout_tab()
mf.tap(2472, 120, pause=1.0)
mf.tap_label(r"^Start Empty Workout$", pause=2.0)
mf.tap_label(r"^Start Workout$", pause=2.0)
out = {}
try:
    I.chooser_add(targets)
    for i, t in enumerate(targets):
        nodes = I.goto_tab(i, t)
        raw = [n.label for n in sorted([n for n in nodes if n.label and 590 < n.c[1] < 640 and n.c[0] > 1000 and n.label != "Set"], key=lambda n: n.c[0])]
        out[t] = {"headers": raw}
        try:
            hs = I.headers(nodes); I.remember_columns(t, hs)
        except Exception as e:
            out[t]["err"] = str(e); continue
        if t in tests:
            s = {"row": "probe", "type": "S", "lb": None, "reps": None, "seconds": None, "dist": None, "rir": None, "felt": None, "note": ""}
            s.update(tests[t])
            b = {"source_exercise": "probe", "target": t, "map": {}, "sets": [s], "notes": [], "extra_notes": [], "header": []}
            try:
                I.enter_block(i, b); out[t]["test"] = f"logged {tests[t]}"
            except Exception as e:
                out[t]["test"] = f"FAILED {tests[t]}: {e}"
finally:
    print(json.dumps(out, ensure_ascii=False))
    I.discard_importer_draft()

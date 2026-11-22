"""Resumable MacroFactor importer driven over ADB.

usage:
  python importer.py plan <workout_id>        print the planned entry (no device access)
  python importer.py run [N] [--id ID ...]    import next N pending workouts (default 1)
  python importer.py verify <workout_id>      re-verify a saved workout against the plan
Stops on the first mismatch or UI surprise; the ledger keeps the state. Create data/STOP to halt
cleanly before the next workout.

Screen coordinates are for a Google Pixel Tablet (2560x1600, landscape). See README.md.
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import mf

HERE = os.environ.get("MF_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(HERE, exist_ok=True)
P = lambda n: os.path.join(HERE, n)  # noqa: E731
LOGF = open(P("import.log"), "a", encoding="utf-8")
LB_PER_KG = 2.20462262185
NOTE_LIMIT = 300


def log(*a):
    s = datetime.now().strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(s, flush=True)
    LOGF.write(s + "\n")
    LOGF.flush()


class Mismatch(RuntimeError):
    pass


# ---------------------------------------------------------------- planning

MANIFEST = json.load(open(P("manifest.json"), encoding="utf-8"))
WORKOUTS = {w["id"]: w for w in MANIFEST["workouts"]}
MAP = json.load(open(P("mapping.json"), encoding="utf-8")) if os.path.exists(P("mapping.json")) else {}
CFG = json.load(open(P("config.json"), encoding="utf-8")) if os.path.exists(P("config.json")) else {}
# Date (as MacroFactor's history shows it, e.g. "Jan 1, 2026") of the oldest workout already in the app.
# Every imported workout must be older; the importer finds its own entries just below this one.
ANCHOR = CFG.get("history_anchor", "")


def lb_value(r):
    """Logged load in lb, snapped only within the source's recorded precision."""
    if r.get("weight") in ("", None):
        return None
    w = float(r["weight"])
    if r["weight_unit"] == "lb":
        return round(w, 2)
    lb = float(r["weight_lb_exact_conversion"])
    tol = 1e-4 if len(r["weight"].split(".")[-1]) > 3 else 0.0111  # 0.005 kg = 0.011 lb
    for step in (1, 0.5, 0.25):
        snapped = round(lb / step) * step
        if abs(lb - snapped) <= tol:
            return snapped
    return round(lb, 2)


def fmt_num(x):
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s


def rir_for(rpe):
    if rpe in ("", None):
        return None
    table = CFG.get("rpe_to_rir", {})
    key = fmt_num(float(rpe))
    if key in table:
        return table[key]
    raise Mismatch(f"no RIR rule for RPE {rpe}")


def start_end(w):
    ov = OVERRIDES.get(w["id"], {}).get("_session")
    if ov:  # reconstructed sessions whose timer was left running or barely ran
        return datetime.fromisoformat(ov["start"]), ov["dur"]
    rec = datetime.fromisoformat(w["recorded_datetime"].split(".")[0])
    dur = int(float(w["duration_seconds_values"][0])) if w["duration_seconds_values"] else None
    if w["source"] == "progression":
        return (rec - timedelta(seconds=dur or 0)), dur
    return rec, dur


OVERRIDES = json.load(open(P("overrides.json"), encoding="utf-8")) if os.path.exists(P("overrides.json")) else {}
REP_TARGET_RE = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*reps?\s*$", re.I)


def add_exercise_note(b, note):
    match = REP_TARGET_RE.fullmatch(note)
    if not match:
        b["notes"].append(note)
        return
    target = (int(match.group(1)), int(match.group(2)))
    if target[0] > target[1]:
        raise Mismatch(f"invalid rep target {note!r}")
    if b["rep_target"] not in (None, target):
        raise Mismatch(f"conflicting rep targets for {b['source_exercise']}: {b['rep_target']} and {target}")
    b["rep_target"] = target


def plan(w):
    """Blocks of contiguous sets per target exercise, with notes built from source comments only."""
    blocks = []
    pending_notes = []  # exercise-note rows seen before the block's first set

    def block_for(src, target, m):
        if not blocks or blocks[-1]["source_exercise"] != src or blocks[-1]["target"] != target:
            blocks.append({"source_exercise": src, "target": target, "map": m, "sets": [], "notes": [],
                           "extra_notes": [], "rep_target": None})
        return blocks[-1]

    for r in w["rows"]:
        m = MAP.get(r["exercise"])
        if not m or not m.get("target"):
            raise Mismatch(f"unmapped exercise {r['exercise']!r}")
        if r["kind"] != "performance":
            if r.get("notes", "").strip():
                if blocks and blocks[-1]["source_exercise"] == r["exercise"]:
                    add_exercise_note(blocks[-1], r["notes"].strip())
                else:
                    pending_notes.append((r["exercise"], r["notes"].strip()))
            continue
        so = r["source_set_order"]
        lb = lb_value(r)
        target = m["target"]
        set_note = []
        felt = None
        if m.get("mode") == "bw_range":
            if lb is None or lb == 0:
                lb = None
            elif m["bw_low"] <= lb <= m["bw_high"]:
                lb = None
            elif lb < m["bw_low"]:
                target = m["assisted_target"]
                felt = lb
                lb = round(BW_PARTS.get(target, CFG["app_bw"]) - felt, 1)  # app shows effective load = its bodyweight share - assistance
            else:
                raise Mismatch(f"{r['exercise']}: load {lb} above bodyweight range")
        elif lb == 0 and m.get("zero_load_is_bodyweight"):
            lb = None
        if lb is None and r.get("reps") not in ("", None) and m.get("mode") != "bw_range":
            if m.get("loadless_target"):  # unloaded sets go to a bodyweight variant
                target = m["loadless_target"]
            elif "Lb" in COLCACHE.get(target, []):  # target requires a load; enter 0 lb
                lb = 0
        b = block_for(r["exercise"], target, m)
        for ex, n in [x for x in pending_notes if x[0] == r["exercise"]]:
            add_exercise_note(b, n)
        pending_notes = [x for x in pending_notes if x[0] != r["exercise"]]
        rpe = r.get("rpe")
        if rpe not in ("", None) and fmt_num(float(rpe)) in CFG.get("rpe_note_for", []):
            set_note.append(f"RPE {fmt_num(float(rpe))}")
        s = {"row": r["id"], "type": {"W": "W", "F": "F"}.get(so, "S"), "lb": lb,
             "reps": int(float(r["reps"])) if r.get("reps") not in ("", None) else None,
             "seconds": int(float(r["seconds"])) if r.get("seconds") not in ("", None) else None,
             "dist": None, "rir": rir_for(rpe), "felt": felt}
        if r.get("distance") not in ("", None):
            if r.get("distance_unit") != "km":
                raise Mismatch(f"unexpected distance unit {r.get('distance_unit')}")
            s["dist"] = fmt_num(float(r["distance"]) * CFG["km_to_mi"])
        if s["dist"] is None and s["seconds"] and m.get("est_pace_min_per_km"):  # target requires a distance; estimate it from time
            s["dist"] = fmt_num(s["seconds"] / 60 / m["est_pace_min_per_km"] * CFG["km_to_mi"])
            set_note.append("distance estimated")
        b["sets"].append(s)
        if r.get("notes", "").strip():
            set_note.append(r["notes"].strip())
        s["note"] = "; ".join(set_note)
    if pending_notes:
        raise Mismatch(f"orphan exercise notes: {pending_notes}")
    for b in blocks:  # warm-ups never follow working sets, so a later W is a logging slip in the source
        t = [st["type"] for st in b["sets"]]
        first_work = next((k for k, x in enumerate(t) if x != "W"), len(t))
        if "W" in t[first_work:]:
            # misplaced warm-ups go first, ahead of the warm-ups logged before the work
            late = [st for st in b["sets"][first_work:] if st["type"] == "W"]
            b["sets"] = late + [st for st in b["sets"] if st["type"] == "W" and all(st is not x for x in late)] \
                + [st for st in b["sets"] if st["type"] != "W"]
            b["corrections"] = [f"warm-up(s) logged after working sets moved first: {''.join(t)} -> "
                                f"{''.join(st['type'] for st in b['sets'])}"]
    for i, b in enumerate(blocks):
        b["header"] = []
        if i == 0:
            b["header"] = [CFG["session_note_prefix"] + n.strip()
                           for n in dict.fromkeys(w["session_notes"]) if n.strip()]
        compose_note(b)
    for b in blocks:  # per-workout corrections from overrides.json
        for k, v in OVERRIDES.get(w["id"], {}).get(b["target"], {}).items():
            for st in b["sets"]:
                st[k] = v
    return blocks


def compose_note(b):
    parts = list(b["header"]) + list(b["notes"])
    for k, s in enumerate(b["sets"], 1):
        bits = [x for x in (s["note"], *s.get("extra", [])) if x]
        if bits:
            parts.append(f"Set {k}: " + "; ".join(bits))
    b["note"] = " | ".join(parts)
    if len(b["note"]) > NOTE_LIMIT:
        raise Mismatch(f"note too long ({len(b['note'])}) for {b['target']}")


# ---------------------------------------------------------------- ledger

DB = sqlite3.connect(P("progress.sqlite3"))


def set_status(wid, status, detail=None, ident=None):
    DB.execute("update workouts set status=?, verification_json=coalesce(?, verification_json),"
               " target_identity=coalesce(?, target_identity), last_update=datetime('now','localtime')"
               " where source_id=?", (status, json.dumps(detail) if detail is not None else None, ident, wid))
    DB.commit()


def sync_check(wids, wait=12):
    """Return to real time so queued writes upload now, and halt if the server rejects any.
    (Writes queued under a past clock were later rejected in bulk and silently reverted.)"""
    mf.sh("logcat -c")
    mf.set_clock(datetime.now())
    mf.clock_auto()
    time.sleep(wait)
    out = mf.sh("logcat -d | grep -iE 'PERMISSION_DENIED|Write failed' | grep -v adbd | head -5")
    if out.strip():
        for wid in wids:
            set_status(wid, "sync_rejected")
        raise SystemExit(f"server rejected write after {wids}: {out.strip()[:500]}")
    log(f"   synced {len(wids)} workout(s) at real time")


def pending(n, ids=None):
    if ids:
        return ids
    rows = DB.execute("select source_id from workouts where status in ('pending','entering')").fetchall()
    ids = [r[0] for r in rows]
    ids.sort(key=lambda i: start_end(WORKOUTS[i])[0])
    return ids[:n]


# ---------------------------------------------------------------- UI helpers

RIR_X = [589, 787, 984, 1181, 1379, 1576, 1773]  # RIR 0..5, 6+ (row shown under the set)
RIR_Y = 972
KEY_OK = (1907, 1423)        # keypad bottom-right (→ on weight, ✓ on last field)
TAB_Y, TAB_X0, TAB_DX = 236, 544, 120


def dump():
    return mf.dump()


def labels(nodes):
    return [n.label for n in nodes if n.label]


def has(nodes, pattern):
    return mf.find(nodes, pattern)


def recover_focus():
    """Handle states invisible to app-only dumps: ANR dialog, or the app no longer in front.
    Returns True if it acted (caller should re-dump)."""
    focus = mf.adb("shell", "dumpsys window | grep mCurrentFocus", timeout=20)
    focus = "\n".join(l for l in focus.splitlines() if "=null" not in l)
    if "Not Responding" in focus:
        log("app not responding; tapping Wait")
        mf.tap(928, 882, pause=5.0)
        return True
    if focus and mf.APP_PKG not in focus:
        log(f"app not in front ({focus.strip()[-80:]}); relaunching")
        mf.adb("shell", f"am start -n {mf.APP_PKG}/.MainActivity", timeout=30)
        time.sleep(8)
        return True
    return False


def to_workout_tab():
    for _ in range(10):
        if recover_focus():
            continue
        nodes = dump()
        if len(labels(nodes)) == 0:  # app still loading (blank) - wait instead of pressing Back
            time.sleep(3)
            continue
        if has(nodes, r"^Cycle Workouts\n") and has(nodes, r"^Dashboard$"):  # Dashboard also matches "Workouts"
            mf.tap(80, 640, pause=1.5)
            continue
        if any(n.c == (2472, 120) or (2440 < n.c[0] < 2500 and n.c[1] < 160 and n.clickable) for n in nodes) \
                and has(nodes, r"^Workout$|Workouts" + (("|" + CFG["workout_tab_regex"]) if CFG.get("workout_tab_regex") else "")):
            return
        if has(nodes, r"^Exit$"):
            raise Mismatch("unexpected unsaved-editor prompt")
        mf.key(4, pause=1.0)
    raise Mismatch("could not reach Workout tab")


def open_history():
    mf.tap(2472, 120, pause=1.0)
    mf.tap_label(r"^Workout History$", pause=2.0)


FLINGS_F = P("history-flings.json")


def history_items_after_anchor(max_scrolls=60):
    """Scroll history to the anchor and return the labels of items below it (newest first)."""
    if not ANCHOR:
        raise SystemExit('set "history_anchor" in data/config.json (see README.md)')
    anchor_dt = datetime.strptime(ANCHOR, "%b %d, %Y")
    last = None
    # Anchor sits a fixed distance from the top: fast flings cover most of it (learned count that stays
    # short of the anchor), then exact momentum-free drags finish the approach.
    learned = json.load(open(FLINGS_F)) if os.path.exists(FLINGS_F) else 0
    for _ in range(learned):
        mf.swipe(1280, 1450, 1280, 300, ms=60, pause=0.12)
    flings, drags, first = learned, 0, True
    for _ in range(max_scrolls):
        nodes = dump()
        items = [n for n in nodes if n.label and re.match(r"^\w{3} \d+, \d{4}\n", n.label)]
        sig = [n.label for n in items]
        idx = next((i for i, n in enumerate(items) if n.label.startswith(ANCHOR + "\n")), None)
        if idx is not None:
            below = items[idx + 1:]
            if below or sig == last:  # sig unchanged => bottom of list, nothing imported yet
                if drags >= 3:
                    flings += 1  # still well short after the flings; one more next time
                if flings != learned:
                    json.dump(flings, open(FLINGS_F, "w"))
                return below, nodes
            mf.swipe(1280, 1300, 1280, 900, ms=500, pause=0.8)
        else:
            dates = [datetime.strptime(l.split("\n")[0], "%b %d, %Y") for l in sig]
            if dates and min(dates) > anchor_dt:
                if learned == 0 and drags < 50:
                    mf.swipe(1280, 1450, 1280, 300, ms=60, pause=0.3)  # first run: count flings
                    flings += 1
                else:
                    mf.drag(1280, 1450, 300, ms=250, pause=0.05)  # still in newer, pre-existing history
                    drags += 1
            elif dates and max(dates) < anchor_dt:
                mf.drag(1280, 300, 1450, ms=250, pause=0.05)  # overshot into imports; back up
                if first:
                    flings -= 1  # flings went too far; use one fewer next time
            else:
                mf.swipe(1280, 1300, 1280, 900, ms=500, pause=0.8)
        last, first = sig, False
    raise Mismatch("anchor not found in history")


def latest_imported():
    """(label,node) of the history item just below the anchor, or None."""
    for attempt in range(2):
        below, nodes = history_items_after_anchor()
        if below:
            return below[0]
        mf.swipe(1280, 1300, 1280, 700, ms=500, pause=0.8)
    return None


def open_latest(w):
    """Open the newest imported workout's detail page, checking it is this workout's date."""
    want = hist_date(work_date(w)) + "\n"
    top = latest_imported()
    for _ in range(3):
        if top is None or not top.label.startswith(want):
            raise Mismatch(f"latest imported workout is {top.label[:40] if top else None!r}, expected {want!r}")
        if top.c[1] < 1250:
            break
        mf.drag(1280, 1100, 700, ms=250, pause=0.3)  # bring a card hidden at the bottom edge into view
        top = next((n for n in dump() if n.label and n.label.startswith(want)), None)
    mf.tap_node(top, pause=2.0)
    for _ in range(4):
        if any(n.label == want.strip() and n.c[1] < 160 for n in dump()):  # detail header shows the date
            return
        time.sleep(1.0)
    raise Mismatch("workout detail did not open")


def open_imported_workout(w):
    """Find an imported workout by date using short, direction-aware history scrolls."""
    want = hist_date(work_date(w)) + "\n"
    target_date = datetime.fromisoformat(work_date(w)).date()
    _, nodes = history_items_after_anchor()
    names = []
    for b in plan(w):
        if not names or names[-1] != b["target"]:
            names.append(b["target"])
    names = ", ".join(names)

    def same_exercises(label):
        line = label.rsplit("\n", 1)[-1]
        return line == names or (line.endswith("...") and names.startswith(line[:-3]))

    for _ in range(100):
        matches = [n for n in nodes if n.label and n.label.startswith(want)]
        header = next((n for n in nodes if n.label == want.strip()), None)
        if header:  # several workouts on one date: the date becomes a separate header node
            later = [n.c[1] for n in nodes if n.label and n.c[1] > header.c[1] + 100
                     and re.match(r"^\w{3} \d+, \d{4}\n", n.label)]
            limit = min(later, default=1600)
            matches = [n for n in nodes if n.clickable and n.label and header.c[1] - 150 < n.c[1] < limit
                       and same_exercises(n.label)] or matches
        if matches:
            for card in matches:
                if card.c[1] >= 1250:
                    mf.drag(1280, 1100, 700, ms=250, pause=0.3)
                    nodes = dump()
                    card = next((n for n in nodes if n.label and n.label == card.label), None)
                    if card is None:
                        break
                mf.tap_node(card, pause=2.0)
                detail = dump()
                if not any(n.label == want.strip() and n.c[1] < 160 for n in detail):
                    raise Mismatch("workout detail did not open")
                if len(matches) == 1:
                    return
                start = next((n for n in collect_detail() if n.startswith("Start Time\n")), "")
                expected_time = start_end(w)[0].strftime("%I:%M %p").lstrip("0")
                if expected_time in start:
                    return
                mf.key(4, pause=1.0)
                nodes = dump()
            else:
                raise Mismatch(f"no workout on {want.strip()} starts at {start_end(w)[0]:%I:%M %p}")
        dates = [datetime.strptime(n.label.split("\n", 1)[0], "%b %d, %Y").date()
                 for n in nodes if n.label and re.match(r"^\w{3} \d+, \d{4}\n", n.label)]
        if not dates:
            mf.swipe(1280, 1300, 1280, 900, ms=500, pause=0.8)
        elif target_date < min(dates):
            mf.swipe(1280, 1300, 1280, 900, ms=500, pause=0.8)
        elif target_date > max(dates):
            mf.drag(1280, 900, 1100, ms=250, pause=0.3)
        else:
            raise Mismatch(f"history shows {target_date} but no matching workout card")
        nodes = dump()
    raise Mismatch(f"workout on {target_date} not found in history")


def work_date(w):
    return str(start_end(w)[0].date())


def hist_date(d):
    return datetime.fromisoformat(d).strftime("%b %-d, %Y") if os.name != "nt" else \
        datetime.fromisoformat(d).strftime("%b ") + str(int(d[8:10])) + ", " + d[:4]


def current_title(nodes):
    # anchored to the "Set k of n" subtitle: once the set table scrolls, other text can sit at the title's y
    subs = [n for n in nodes if n.label and re.match(r"^Set \d+ of \d+$", n.label) and n.c[0] < 1500]
    if not subs:
        return None
    sub = subs[0]
    t = [n for n in nodes if n.label and not n.clickable and n.c[0] < 1500 and n is not sub
         and 0 < sub.c[1] - n.c[1] < 100 and not n.label.startswith("Set ")]
    return max(t, key=lambda n: n.c[1]).label if t else None


def chooser_add(targets):
    mf.tap(553, 236, pause=1.5)
    nodes = dump()
    if has(nodes, r"^Home$"):
        mf.tap(488, 265, pause=1.0)
        if has(dump(), r"^Home$"):
            raise Mismatch("could not clear Home filter")
    for t in targets:
        for _ in range(8):
            nodes = dump()
            ed = [n for n in nodes if n.cls == "EditText"]
            if ed:
                break
            mf.swipe(1280, 500, 1280, 1300, ms=150, pause=0.6)  # results scroll the search bar off the top
        if not ed:
            raise Mismatch("search field missing")
        q = max(re.split(r"[^ -~]+", t), key=len).strip().lower()  # adb input is ASCII-only
        mf.tap_node(ed[0], pause=0.4)
        if not mf.set_text_edit(q):  # one accessibility set-text call replaces the old query
            mf.key("KEYCODE_MOVE_END", pause=0.05)
            mf.sh("input keyevent " + " ".join(["KEYCODE_DEL"] * 40))
            mf.type_text(q)
        mf.key(4, pause=1.2)
        hit = None
        for k in range(10):  # scan down first; results can keep the previous query's offset, so then scan up
            nodes = dump()
            for n in nodes:
                if n.label and n.c[0] == 1280 and 300 < n.c[1] < 1350:  # bottom area is covered by the selection bar
                    parts = n.label.split("\n")
                    names = [p for p in parts if p == t]
                    if names:
                        hit = n
                        break
            if hit:
                break
            if k < 4:
                mf.swipe(1280, 1250, 1280, 700, ms=600, pause=0.8)
            else:
                mf.swipe(1280, 700, 1280, 1250, ms=600, pause=0.8)
        if not hit:
            raise Mismatch(f"exercise not found in chooser: {t}")
        for _ in range(3):  # the list can still be settling after the keyboard closes; tap a stable row
            again = next((n for n in dump() if n.label == hit.label and n.c[0] == 1280), None)
            if again and again.c == hit.c:
                break
            hit = again or hit
            time.sleep(0.3)
        mf.tap(2040, hit.c[1], pause=0.9)
        sheet = dump()
        if has(sheet, r"^Add Exercise$"):  # tap hit the row body and opened its info sheet; add from there
            if not has(sheet, "^" + re.escape(t) + "$"):
                raise Mismatch(f"chooser opened an info sheet for another exercise while adding {t}")
            log(f"   chooser opened info sheet for {t}; adding from it")
            mf.tap_label(r"^Add Exercise$", pause=1.0)
            if "mInputShown=true" in (mf.sh("dumpsys input_method | grep mInputShown") or ""):
                mf.key(4, pause=0.8)  # closing the sheet focuses the search field; hide only the keyboard
        sel = [n for n in dump() if n.label and n.label.startswith("Your Exercises")]
        if sel and t not in sel[0].label and len(targets) == 1:
            raise Mismatch(f"selection of {t} not confirmed")
    nodes = dump()
    chosen = selected_exercises(nodes)
    if 6 <= len(chosen) < len(targets) and chosen == list(targets)[:len(chosen)]:
        chosen = list(targets)  # panel overflows; tab count and each tab's title are checked during entry
    if chosen != list(targets):
        raise Mismatch(f"chooser selection {chosen} != {list(targets)}")
    done = [n for n in nodes if n.label == "Done" and n.clickable]
    mf.tap_node(done[-1], pause=2.0)
    tabs = [n for n in dump() if n.clickable and abs(n.c[1] - TAB_Y) < 10 and 480 < n.c[0] < 2400]
    if len(tabs) - 1 != len(targets):
        raise Mismatch(f"workout has {len(tabs) - 1} exercises after chooser, expected {len(targets)}")


def selected_exercises(nodes):
    """Names listed under the chooser's 'Your Exercises' panel, in selection order."""
    out, on = [], False
    for n in nodes:
        if n.label and n.label.startswith("Your Exercises\n"):  # single selection: panel collapses to one node
            return [p for p in n.label.split("\n")[1:-1:2]]  # name / muscle-line pairs, then a superset prompt
        if n.label == "Your Exercises":
            on = True
        elif on and n.label and (n.label.startswith("Add as a circuit") or n.label in ("Gym", "Done")):
            break  # long selections push the circuit button off-screen; the filter panel follows
        elif on and n.cls in ("ImageView", "View") and n.clickable and n.label:  # exercises without art render as View
            out.append(next(p for p in n.label.split("\n") if len(p) > 2))
    return out


def discard_importer_draft():
    """Discard an unsaved in-progress workout left behind by an interrupted importer attempt."""
    for _ in range(4):
        nodes = dump()
        if has(nodes, r"^Exit Without Saving\?$"):
            mf.tap_label(r"^Exit$", pause=1.5)
            continue
        if has(nodes, r"^Your Exercises$"):
            mf.tap(504, 120, pause=1.5)  # close chooser
            continue
        active = any(n.label and re.match(r"^\d+:\d\d:\d\d$", n.label) and n.c[1] < 160 for n in nodes)  # live timer
        if not active:
            if not any(n.label for n in nodes) or has(nodes, r"^Exercises$|^Done$"):
                time.sleep(1.0)  # transient/empty dump, or a sheet over the workout; look again
                mf.key(4, pause=1.0) if has(nodes, r"^Done$") else None
                continue
            return
        if keypad_open(nodes):
            mf.tap(1907, 1105, pause=0.8)
        mf.tap(64, 116, pause=1.0)
        mf.tap_label(r"^Discard workout$", pause=1.2)
        mf.tap_label(r"^Discard$", pause=1.5)
        log("   discarded unsaved importer draft")
    raise Mismatch("could not discard unsaved draft")


def keypad_open(nodes):
    return any(n.label == "1" and abs(n.c[0] - 653) < 10 and abs(n.c[1] - 1105) < 10 for n in nodes)


def goto_tab(i, target, nodes=None):
    nodes = nodes or dump()
    if keypad_open(nodes):
        mf.tap(1907, 1105, pause=0.8)  # keypad's hide-keyboard key
        nodes = dump()
    t = current_title(nodes)
    sub = next((n for n in nodes if n.label and re.match(r"^Set \d+ of \d+$", n.label) and n.c[0] < 1500), None)
    if t is None or sub is None or abs(sub.c[1] - 435) > 40:
        # page scrolled (keypad or last set); row geometry assumes the unscrolled layout
        mf.drag(1280, 500, 1300, ms=250, pause=0.3)
        nodes = dump()
        t = current_title(nodes)
    if t == target:
        return nodes
    if t is None:
        raise Mismatch("exercise title not visible")
    mf.tap(TAB_X0 + TAB_DX * i, TAB_Y, pause=1.2)
    nodes = dump()
    if current_title(nodes) != target:
        raise Mismatch(f"tab {i} shows {current_title(nodes)!r}, expected {target!r}")
    return nodes


def set_rows(nodes):
    """Rows: list of dicts {btn, fields[EditText...], y} for the visible set table."""
    btns = [n for n in nodes if n.cls == "Button" and 450 < n.c[0] < 600 and n.c[1] > 640]
    rows = []
    for b in btns:
        if b.label in ("",) and not any(abs(e.c[1] - b.c[1]) < 30 for e in nodes if e.cls == "EditText"):
            continue  # the add-set "+" button
        f = sorted([e for e in nodes if e.cls in ("EditText",) and abs(e.c[1] - b.c[1]) < 30], key=lambda e: e.c[0])
        rows.append({"btn": b, "fields": f, "y": b.c[1]})
    return rows


def headers(nodes):
    """Column headers, normalised: per-side columns (dumbbells) take logged values as-is."""
    hs = [n for n in nodes if n.label and 590 < n.c[1] < 640 and n.c[0] > 1000 and n.label not in ("Set",)]
    out = []
    for h in sorted(hs, key=lambda n: n.c[0]):
        lab = h.label.replace("/Side", "")
        if lab not in ("Lb", "Reps", "Time", "Mi", "Kg", "km", "Yd"):
            raise Mismatch(f"unknown column header {h.label!r}")
        out.append(lab)
    return out


def add_set_button(nodes):
    btns = [n for n in nodes if n.cls == "Button" and 450 < n.c[0] < 600 and n.c[1] > 640 and not n.label]
    return max(btns, key=lambda n: n.c[1]) if btns else None


def time_digits(sec):
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}{m:02d}{s:02d}" if h else f"{m}{s:02d}"


COLS = {"lb": "Lb", "reps": "Reps", "seconds": "Time", "dist": "Mi"}


def field_value(h, s):
    if h == "Lb":
        return None if s["lb"] is None else fmt_num(s["lb"])
    if h == "Reps":
        return None if s["reps"] is None else str(s["reps"])
    if h == "Time":
        return None if s["seconds"] is None else time_digits(s["seconds"])
    if h == "Mi":
        return s["dist"]
    if h == "Yd":  # carries logged as reps go into Yd
        return None if s["reps"] is None else str(s["reps"])
    raise Mismatch(f"unsupported column {h}")


def describe(k, s):
    return {"lb": lambda: f"{fmt_num(s['lb'])} lb", "reps": lambda: f"{s['reps']} reps",
            "seconds": lambda: f"{s['seconds']} s", "dist": lambda: f"{s['dist']} mi"}[k]()


def fit_columns(hs, b):
    """Values with no target column move into the set's note."""
    cols = set(hs)
    unknown = cols - set(COLS.values()) - {"Yd"}
    if unknown:
        raise Mismatch(f"{b['target']}: unknown columns {unknown}")
    for s in b["sets"]:
        s["moved"] = [k for k, col in COLS.items() if s[k] is not None and col not in cols
                      and not (k == "reps" and "Yd" in cols)]
        s["extra"] = [describe(k, s) for k in s["moved"]]
        s["yd"] = "Yd" in cols
        if not any(field_value(h, s) for h in hs):
            raise Mismatch(f"{b['target']}: set {s['row']} has no value for columns {hs}")
    compose_note(b)


def enter_block(i, b):
    nodes = goto_tab(i, b["target"])
    hs = headers(nodes)
    remember_columns(b["target"], hs)
    unit = next((u for u in ("km", "Yd") if u in hs), None)
    if unit and (unit == "km" or any(s["dist"] is not None for s in b["sets"])):  # distances are entered in miles
        mf.tap_node([n for n in nodes if n.label == unit and 590 < n.c[1] < 640][0], pause=0.8)
        mf.tap_label(r"^Mi$", pause=1.0)
        nodes = goto_tab(i, b["target"])
        hs = headers(nodes)
        remember_columns(b["target"], hs)
    apply_bw_part(nodes, b)
    fit_columns(hs, b)
    if b["note"]:  # before logging, so Exercise Auto-Next never forces a switch back
        write_note(i, b)
        nodes = goto_tab(i, b["target"])
    for _ in range(12):
        rows = set_rows(nodes)
        if len(rows) <= len(b["sets"]):
            break
        if not rows[-1]["fields"]:
            raise Mismatch(f"{b['target']}: surplus row is already logged")
        y = rows[-1]["y"]
        mf.swipe(1500, y, 200, y, ms=500, pause=1.5)  # app pre-fills rows from history
        nodes = dump()
        # A swipe sometimes only reveals the row's red delete button; tap it to finish the delete.
        red = [n for n in nodes if n.clickable and not n.label and 2000 < n.c[0] < 2120 and abs(n.c[1] - y) < 30]
        if red:
            mf.tap_node(red[0], pause=1.0)
            nodes = dump()
    for _ in range(12):
        rows = set_rows(nodes)
        if len(rows) >= len(b["sets"]):
            break
        mf.tap_node(add_set_button(nodes), pause=0.7)
        nodes = dump()
    rows = set_rows(nodes)
    if len(rows) != len(b["sets"]):
        raise Mismatch(f"{b['target']}: have {len(rows)} rows, need {len(b['sets'])}")
    write_rep_target(i, b)
    set_row_types(i, b)
    nodes = None
    for k, s in enumerate(b["sets"]):
        nodes = goto_tab(i, b["target"], nodes)
        row = set_rows(nodes)[k]
        if not row["fields"]:
            raise Mismatch(f"{b['target']}: row {k + 1} already logged before entry")
        vals = [field_value(h, s) for h in hs]
        mf.tap_node(row["fields"][0], pause=0.6)
        # One adb call per set using the app keypad: value, OK (-> next field), ..., RIR, OK (logs the set).
        # The RIR row appears only after reps are typed, hence the on-device sleep before tapping it.
        # Any slip is caught by the logged-row check below and by the full detail verification.
        cmds = []
        for j, v in enumerate(vals):
            if v is None:
                cmds.append("input keyevent 67")
            else:
                cmds.append("input text " + mf._quote(str(v)))
            cmds += last_field_tail(j == len(vals) - 1 and hs[j] == "Reps", s)
            if j < len(vals) - 1:
                cmds.append("sleep 0.2")  # 0.1 once dropped the first digit typed into the next field
        mf.sh("; ".join(cmds))
        nodes = dump()
        if k == len(b["sets"]) - 1 and current_title(nodes) not in (b["target"], None):
            break  # Auto-Next advanced, which only happens once every set is logged
        nodes = goto_tab(i, b["target"], nodes)
        row = set_rows(nodes)[k]
        if row["fields"]:
            # the final OK can outrun the app; if every field already holds its value, retype the
            # last one (focus selects it) and log again
            if [f.text.strip() for f in row["fields"]] != ["" if v is None else str(v) for v in vals] \
                    or vals[-1] is None:
                raise Mismatch(f"{b['target']}: row {k + 1} did not log")
            log(f"   row {k + 1} of {b['target']} needed a second OK")
            mf.tap_node(row["fields"][-1], pause=0.5)
            mf.sh("; ".join(["input text " + mf._quote(str(vals[-1]))]
                            + last_field_tail(hs[-1] == "Reps", s)))
            nodes = goto_tab(i, b["target"])
            if k < len(set_rows(nodes)) and set_rows(nodes)[k]["fields"]:
                raise Mismatch(f"{b['target']}: row {k + 1} did not log")


def last_field_tail(is_reps, s):
    """Keypad taps after typing a field's value: optional RIR, then OK."""
    tail = []
    if is_reps and s["rir"] is not None:
        wait = 0.5 if s["type"] == "F" else 0.25  # failure rows were seen to miss the RIR tap
        tail.append(f"sleep {wait}; input tap {RIR_X[min(int(s['rir']), 6)]} {RIR_Y}")
    tail.append(f"sleep 0.15; input tap {KEY_OK[0]} {KEY_OK[1]}")
    return tail


def write_note(i, b, clear_empty=False):
    if not b["note"] and not clear_empty:
        return
    nodes = goto_tab(i, b["target"])
    note = [n for n in nodes if n.label == "Note" and abs(n.c[1] - 511) < 30]
    mf.tap_node(note[0], pause=1.2)
    nodes = dump()
    if not has(nodes, r"^Session Note$"):
        raise Mismatch("note dialog not on Session tab")
    ed = [n for n in nodes if n.cls == "EditText"][0]
    mf.tap_node(ed, pause=0.5)
    if not mf.set_text_edit(b["note"]):  # one set-text call: no keyboard autocorrect/capitalisation, any characters
        mf.key("KEYCODE_MOVE_END", pause=0.05)
        mf.sh("input keyevent " + " ".join(["KEYCODE_DEL"] * (NOTE_LIMIT + 20)))
        mf.type_text(b["note"], pause=0.5)
    mf.tap_label(r"^Save$", pause=1.0)


def rep_target_fields(nodes):
    fields = [n for n in nodes if n.cls == "EditText" and n.clickable
              and (800 <= n.c[0] <= 1220 or 1400 <= n.c[0] <= 1740)
              and 250 <= n.c[1] <= 1450]
    return sorted(fields, key=lambda n: (n.c[1], n.c[0]))


def write_rep_target(i, b):
    target = b.get("rep_target")
    if not target:
        return
    if any(s["reps"] is None for s in b["sets"]):
        raise Mismatch(f"{b['target']}: rep target cannot be applied to a set without repetitions")

    nodes = goto_tab(i, b["target"])
    button = next((n for n in nodes if n.label == "Targets" and n.clickable and 450 < n.c[1] < 700), None)
    if not button:
        raise Mismatch(f"{b['target']}: Targets control not found")
    mf.tap_node(button, pause=1.0)
    nodes = dump()
    if not has(nodes, r"Workout Targets"):
        raise Mismatch(f"{b['target']}: Workout Targets dialog did not open")

    needed = 2 * len(b["sets"])
    seen = 0  # highest row number seen; visible fields undercount once the dialog scrolls row 1 away
    for _ in range(2 * len(b["sets"]) + 2):
        fields = rep_target_fields(nodes)
        seen = max([seen, len(fields) // 2] + [int(n.label) for n in nodes if n.cls == "Button"
                                               and n.label.isdigit() and 700 <= n.c[0] <= 850])
        if seen == len(b["sets"]):
            break
        if seen > len(b["sets"]):
            raise Mismatch(f"{b['target']}: {seen} target rows, expected {len(b['sets'])}")
        last_y = fields[-1].c[1] if fields else 680
        plus = next((n for n in nodes if n.clickable and not n.label and 700 <= n.c[0] <= 850
                     and last_y < n.c[1] < 1450), None)
        if not plus:
            mf.swipe(1280, 1300, 1280, 800, ms=400, pause=0.5)
            nodes = dump()
            continue  # recount: the scroll may reveal rows that already exist
        mf.tap_node(plus, pause=0.5)
        nodes = dump()
        if not has(nodes, r"Workout Targets"):
            raise Mismatch(f"{b['target']}: Workout Targets dialog closed while adding rows")
    fields = rep_target_fields(nodes)
    if len(fields) != needed:  # adding rows scrolled the dialog; return to the top so every row is in view
        for _ in range(4):  # short momentum-free drags: overscrolling at the top drags the sheet closed
            if any(n.cls == "Button" and n.label == "1" and 700 <= n.c[0] <= 850 for n in nodes):
                break
            mf.drag(1280, 800, 1100, ms=300, pause=0.4)
            nodes = dump()
        if not has(nodes, r"Workout Targets"):
            raise Mismatch(f"{b['target']}: Workout Targets dialog closed while scrolling")
        fields = rep_target_fields(nodes)
    if len(fields) != needed:
        raise Mismatch(f"{b['target']}: {len(fields) // 2} target rows, expected {len(b['sets'])}")

    # One tap on row 1 Min, then a single adb call types every row. Focus order per row is
    # Min -> Max -> RIR -> next set-number button -> next Min, so rows are joined by three Tabs.
    # Focusing a field selects its contents, so typing replaces any copied value. Enter (on a Max
    # field) closes the keyboard; Back would dismiss the whole dialog.
    for f in fields:
        current = f.text.strip()
        if current and current != "Optional" and not current.isdigit():
            raise Mismatch(f"{b['target']}: unexpected target field text {current!r}")
    mf.tap_node(fields[0], pause=0.4)
    row = f"input text {target[0]}; input keyevent 61; input text {target[1]}"
    for k in range(len(b["sets"])):
        # separate calls + pause: tabbing onto an off-screen row scrolls the dialog, and keys sent
        # during that scroll are dropped
        mf.sh(row if k == 0 else "input keyevent 61 61 61; " + row)
        time.sleep(0.25)
    want = [str(target[k % 2]) for k in range(needed)]
    for _ in range(2):  # repair pass: re-read positions, tap (selects contents) and retype wrong fields
        fields = rep_target_fields(dump())
        bad = [k for k, f in enumerate(fields) if f.text.strip() != want[k]]
        if len(fields) != needed or not bad:
            break
        for k in bad:
            f = rep_target_fields(dump())[k]
            mf.tap_node(f, pause=0.3)
            mf.type_text(want[k], pause=0.3)
    # close MacroFactor's in-app keypad (not an IME, so mInputShown can't see it) with its own
    # check key; Back would dismiss the whole dialog
    for _ in range(4):
        nodes = dump()
        keypad = [n for n in nodes if n.cls == "View" and n.clickable and n.c[1] > 1000
                  and n.label in "0123456789" and n.label]
        if not keypad:
            break
        bottom = max(n.c[1] for n in keypad)
        ok = max((n for n in nodes if n.clickable and abs(n.c[1] - bottom) < 30 and not n.label),
                 key=lambda n: n.c[0], default=None)
        if not ok:
            raise Mismatch(f"{b['target']}: keypad check key not found")
        mf.tap_node(ok, pause=0.4)
    else:
        raise Mismatch(f"{b['target']}: keypad would not close in Targets dialog")
    nodes = dump()
    if not has(nodes, r"Workout Targets"):
        raise Mismatch(f"{b['target']}: target dialog closed before saving")
    final = rep_target_fields(nodes)
    if [f.text.strip() for f in final] != want:
        raise Mismatch(f"{b['target']}: target rows {[f.text.strip() for f in final]} != {want}")
    mf.tap_label(r"^Save$", pause=1.0)
    nodes = dump()
    range_re = re.compile(rf"\b{target[0]}\s*[–-]\s*{target[1]}\b")
    if not any(n.label and range_re.search(n.label) for n in nodes):
        raise Mismatch(f"{b['target']}: saved rep target {target[0]}-{target[1]} not visible")


def verify_rep_targets(blocks):
    targets = [(i, b) for i, b in enumerate(blocks) if b.get("rep_target")]
    if not targets:
        return False
    # the row sits just below the muscle map; the detail check leaves the page at the bottom
    for _ in range(3):
        mf.swipe(1280, 400, 1280, 1450, ms=120, pause=0.3)  # fling to top
    for _ in range(6):
        edit = [n for n in dump() if n.label and n.label.startswith("Edit Workout\n")]
        if edit:
            break
        mf.drag(1280, 1300, 600, ms=500, pause=0.3)
    if edit and edit[0].c[1] > 1100:  # bottom Done bar (y>1310) hides it; the Duration Edit sits just above
        mf.drag(1280, 1100, 700, ms=500, pause=0.3)
        edit = [n for n in dump() if n.label and n.label.startswith("Edit Workout\n")]
    if not edit or not 300 < edit[0].c[1] < 1150:
        raise Mismatch("Edit Workout control not found for rep-target verification")
    mf.tap(2025, edit[0].c[1], pause=0.3)
    try:
        mf.wait_for(r"^Edit Workout$", timeout=8)
    except mf.UIError:
        pass
    if not has(dump(), r"^Edit Workout$"):
        raise Mismatch("workout editor did not open for rep-target verification")
    for i, b in targets:
        nodes = goto_tab(i, b["target"])
        rows = set_rows(nodes)
        if len(rows) < len(b["sets"]):
            raise Mismatch(f"{b['target']}: only {len(rows)} rows visible while verifying targets")
        lo, hi = b["rep_target"]
        range_re = re.compile(rf"\b{lo}\s*[–-]\s*{hi}\b")
        for row, s in zip(rows, b["sets"]):
            if s["type"] == "W":  # MacroFactor clears targets when a set becomes a warm-up
                continue
            row_labels = [n.label for n in nodes if n.label and abs(n.c[1] - row["y"]) < 30]
            if not any(range_re.search(label) for label in row_labels):
                raise Mismatch(f"{b['target']}: rep target {lo}-{hi} not saved on set row {row['y']}")
    mf.tap(2487, 115, pause=1.0)
    return True


def fix_notes(wid):
    """Rewrite exercise notes and rep targets of an imported workout, then re-verify."""
    w = WORKOUTS[wid]
    blocks = plan(w)
    for b in blocks:
        fit_columns(COLCACHE[b["target"]], b)
    to_workout_tab()
    open_history()
    open_imported_workout(w)
    for _ in range(4):
        e = [n for n in dump() if n.label and n.label.startswith("Edit Workout\n")]
        if e:
            break
        mf.swipe(1280, 1400, 1280, 700, ms=500, pause=1.0)
    mf.tap(1977, e[0].c[1], pause=2.5)
    if not has(dump(), r"^Edit Workout$"):
        raise Mismatch("workout editor did not open")
    for i, b in enumerate(blocks):
        if b["note"] or b.get("rep_target"):
            write_note(i, b, clear_empty=bool(b.get("rep_target")))
            write_rep_target(i, b)
    mf.tap(2487, 115, pause=2.5)  # editor's save check mark
    log("   rewrote notes via editor")
    verify_saved(wid, already_open=True)


def fix_pending_rep_targets():
    pending = []
    for wid, verification_json in DB.execute(
            "select source_id, verification_json from workouts where status='verified'").fetchall():
        if not any(b.get("rep_target") for b in plan(WORKOUTS[wid])):
            continue
        verification = json.loads(verification_json or "{}")
        if not verification.get("rep_targets_verified"):
            pending.append(wid)
    pending.sort(key=lambda wid: start_end(WORKOUTS[wid])[0], reverse=True)
    for wid in pending:
        log("REP TARGET FIX", wid)
        fix_notes(wid)
    log("rep target fixes complete", len(pending))


def finish(end_dt):
    mf.set_clock(end_dt)
    mf.tap(64, 116, pause=1.0)
    mf.tap_label(r"^Finish workout$", pause=1.5)
    nodes = dump()
    if has(nodes, r"^Log Only Completed Sets$"):
        if not has(nodes, r"You've logged (\d+) of \1 exercises"):
            raise Mismatch("finish prompt reports unlogged exercises: " + " / ".join(labels(nodes)))
        mf.tap_label(r"^Log Only Completed Sets$", pause=3.0)
    if not has(dump(), r"^Workout Complete$"):
        raise Mismatch("Workout Complete screen did not appear")


# ---------------------------------------------------------------- verification

HDR_RE = re.compile(r"\n\d+ sets?$")
ROW_RE = re.compile(r"^(W|F|D|M|\d+)\n")


def _bottom(nodes):
    # Workout Complete screen pins a Done button at y~1400 over the content; History detail runs to the taskbar
    return 1330 if any(n.label == "Done" and n.c[1] > 1300 for n in nodes) else 1520


def _detail_items(nodes):
    bottom = _bottom(nodes)
    return sorted([n for n in nodes if n.label and n.c[0] == 1280 and 160 < n.c[1] < bottom and n.label != "Done"],
                  key=lambda n: n.c[1])


def _to_top():
    if any(n.label == "Workout Details" or n.label.startswith(("Volume\n", "Start Time\n")) for n in dump() if n.label):
        return  # already at the top (freshly opened detail page)
    for _ in range(3):
        mf.swipe(1280, 500, 1280, 1300, ms=150, pause=0.3)


def _collapsed(items, k, at_end):
    """Header k is collapsed when the item after it is not a set row (or nothing follows at the page end)."""
    nxt = items[k + 1] if k + 1 < len(items) else None
    if nxt is None:
        return at_end
    return not (ROW_RE.match(nxt.label) and not HDR_RE.search(nxt.label))  # picture-less headers start with initials


def collect_detail():
    """Single top-to-bottom pass from a freshly opened (top-positioned) page: expand collapsed exercises
    as they come into view, then record labels."""
    seq, last_sig, taps = [], None, {}
    for _ in range(80):
        nodes = dump()
        items = _detail_items(nodes)
        sig = tuple(n.label for n in items)
        at_end = sig == last_sig
        hit = next((k for k, h in enumerate(items) if HDR_RE.search(h.label) and h.c[1] < _bottom(nodes) - 40
                    and _collapsed(items, k, at_end)), None)
        if hit is not None:
            h = items[hit]
            btn = [e for e in nodes if e.cls == "Button" and e.c[0] > 2000 and abs(e.c[1] - h.c[1]) < 40]
            taps[h.label] = taps.get(h.label, 0) + 1
            if not btn or taps[h.label] > 3:
                raise Mismatch(f"could not expand {h.label!r}")
            mf.tap_node(btn[0], pause=0.8)
            last_sig = None
            continue
        labs = list(sig)
        k = next((k for k in range(min(len(seq), len(labs)), 0, -1) if seq[-k:] == labs[:k]), 0)
        if seq and k == 0:
            raise Mismatch("lost scroll overlap while reading detail")
        seq += labs[k:]
        if at_end:
            return seq
        last_sig = sig
        mf.drag(1280, 1200, 500, ms=250, pause=0.2)  # exact, keeps overlap
    raise Mismatch("detail page did not end")


def expected_body(s):
    s = {**s, **{k: None for k in s.get("moved", [])}}  # values moved into the note are not shown in rows
    if s["seconds"] is not None and s["dist"] is not None:
        return f"{time_fmt(s['seconds'])} x {s['dist']} Mi"
    if s["lb"] is not None and s["reps"] is not None:
        return f"{fmt_num(s['lb'])} lb x {s['reps']}" + (" Yd" if s.get("yd") else "")
    if s["reps"] is not None and s["lb"] is None and s["seconds"] is None:
        return f"{s['reps']} reps"
    if s["seconds"] is not None and s["lb"] is None and s["reps"] is None:
        return time_fmt(s["seconds"])
    if s["lb"] is not None and s["reps"] is None and s["seconds"] is None and s["dist"] is None:
        return f"{fmt_num(s['lb'])} lb"
    return " / ".join(str(s[k]) for k in ("lb", "reps", "seconds", "dist") if s[k] is not None)


def time_fmt(sec):
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


MEASURED_BW = {}  # assisted target -> bodyweight share read from the saved detail


def compare_detail(w, blocks, seq):
    problems = []
    start, dur = start_end(w)
    st = next((l for l in seq if l.startswith("Start Time\n")), "")
    du = next((l for l in seq if l.startswith("Duration\n")), "")
    want_t = start.strftime("%I:%M %p").lstrip("0")
    if want_t not in st:
        problems.append(f"start {st!r} != {want_t}")
    if dur and du.split("\n")[1:2] == ["9:59:59+"] and dur >= 35999:
        pass  # app display/edit limit; "+" shows the saved duration exceeds it
    elif dur:
        try:
            h, m, s2 = map(int, du.split("\n")[1].split(":"))
            if abs(h * 3600 + m * 60 + s2 - dur) > 120:
                problems.append(f"duration {du!r} != {dur}s")
        except (IndexError, ValueError):
            problems.append(f"duration unreadable {du!r}")
    got, cur = [], None
    for l in seq:
        if HDR_RE.search(l):
            cur = {"hdr": l, "rows": [], "note": None}
            got.append(cur)
        elif cur and ROW_RE.match(l):
            cur["rows"].append(l)
        elif cur and l.startswith("Session Note\n"):
            cur["note"] = l.split("\n", 1)[1]
    if len(got) != len(blocks):
        problems.append(f"{len(got)} exercises saved, expected {len(blocks)}")
    for b, g in zip(blocks, got):
        if b["target"] not in g["hdr"].split("\n"):
            problems.append(f"exercise {g['hdr']!r} != {b['target']!r}")
            continue
        n, exp = 0, []
        for s in b["sets"]:
            n += s["type"] == "S"
            tag = str(n) if s["type"] == "S" else s["type"]
            e = f"{tag}\n{expected_body(s)}"
            if s["rir"] is not None:
                e += "\n" + ("6+" if s["rir"] >= 6 else str(s["rir"]))
            elif s["type"] == "F":  # app implies RIR 0 for failure sets
                e += "\n0"
            exp.append(e)
        rows = [r.replace(" (per side)", "") for r in g["rows"]]
        if "Assisted" in b["target"]:  # app displays assistance as a negative load
            rows = [re.sub(r"^([^\n]+\n)-", r"\1", r) for r in rows]
            mbw = re.search(r"\(\+([\d.,]+) lb BW\)", g["hdr"])
            reps = sum(s["reps"] or 0 for s in b["sets"])
            if mbw and reps:
                bw = float(mbw.group(1).replace(",", "")) / reps
                MEASURED_BW[b["target"]] = round(bw, 1)
                want_bw = BW_PARTS.get(b["target"], CFG["app_bw"])
                if abs(bw - want_bw) > 0.1:
                    problems.append(f"{b['target']}: app bodyweight {bw:.2f} lb != assumed {want_bw} (assistance values need recomputing)")
            else:
                problems.append(f"{b['target']}: bodyweight volume not shown in {g['hdr']!r}")
        if rows != exp:
            fixes = rir_only_fixes(rows, exp)
            if fixes is not None:
                RIR_FIXES.extend((blocks.index(b), k, have) for k, have in fixes)
                problems.append(f"repairable {b['target']}: rows {rows} != {exp}")
            else:
                problems.append(f"{b['target']}: rows {rows} != {exp}")
        if (g["note"] or "") != b["note"]:
            problems.append(f"{b['target']}: note {g['note']!r} != {b['note']!r}")
    return problems


RIR_FIXES = []  # (block index, row index, shown RIR or None) for rows whose reps/RIR alone differ
RIR_LABELS = ["0", "1", "2", "3", "4", "5", "6+"]
REPS_TAIL = re.compile(r"^(.*?)(\d+)( reps?)?$")


def rir_only_fixes(rows, exp):
    """Rows differing only in reps count and/or RIR -> [(k, shown RIR)]; None if anything else differs."""
    if len(rows) != len(exp):
        return None
    out = []
    for k, (r, e) in enumerate(zip(rows, exp)):
        if r == e:
            continue
        rp, ep = r.split("\n"), e.split("\n")
        if rp[0] != ep[0] or len(rp) > 3 or len(ep) > 3 or len(rp) < 2 or len(ep) < 2:
            return None
        if rp[1] != ep[1]:
            mr, me = REPS_TAIL.match(rp[1]), REPS_TAIL.match(ep[1])
            if not (mr and me and mr.group(1) == me.group(1) and bool(mr.group(3)) == bool(me.group(3))):
                return None
        out.append((k, rp[2] if len(rp) == 3 else None))
    return out


def open_editor_from_detail():
    """From a saved workout's detail/Workout Complete page, open its workout editor."""
    for _ in range(3):
        mf.swipe(1280, 400, 1280, 1450, ms=120, pause=0.3)  # fling to top
    edit = []
    for _ in range(6):
        edit = [n for n in dump() if n.label and n.label.startswith("Edit Workout\n")]
        if edit:
            break
        mf.drag(1280, 1300, 600, ms=500, pause=0.3)
    if edit and edit[0].c[1] > 1100:  # bottom Done bar (y>1310) hides it
        mf.drag(1280, 1100, 700, ms=500, pause=0.3)
        edit = [n for n in dump() if n.label and n.label.startswith("Edit Workout\n")]
    if not edit or not 300 < edit[0].c[1] < 1150:
        raise Mismatch("Edit Workout control not found")
    mf.tap(2025, edit[0].c[1], pause=0.3)
    try:
        mf.wait_for(r"^Edit Workout$", timeout=8)
    except mf.UIError:
        pass
    if not has(dump(), r"^Edit Workout$"):
        raise Mismatch("workout editor did not open")


TYPE_MENU = {"S": r"^Standard Set", "W": r"^Warm-Up Set", "F": r"^Failure Set"}


def row_type(label):
    return "S" if (label or "").isdigit() else (label or "?")


def set_row_types(i, b):
    """Set every (unlogged) row's type explicitly: the app pre-fills rows, including their type, from history."""
    for k, s in enumerate(b["sets"]):
        rows = set_rows(goto_tab(i, b["target"]))
        if row_type(rows[k]["btn"].label) == s["type"]:
            continue
        mf.tap_node(rows[k]["btn"], pause=1.0)
        mf.tap_label(TYPE_MENU[s["type"]], pause=1.0)
        got = row_type(set_rows(goto_tab(i, b["target"]))[k]["btn"].label)
        if got != s["type"]:
            raise Mismatch(f"{b['target']}: row {k + 1} type {got} != {s['type']}")


def fix_types(wid, already_open=True):
    """Correct set types of a saved workout in the editor: un-log bottom-up, set types, re-log top-down."""
    blocks = plan(WORKOUTS[wid])
    open_editor_from_detail()
    for i, b in enumerate(blocks):
        rows = set_rows(goto_tab(i, b["target"]))
        if len(rows) != len(b["sets"]):
            raise Mismatch(f"{b['target']}: {len(rows)} rows visible, need {len(b['sets'])}")
        if all(row_type(r["btn"].label) == s["type"] for r, s in zip(rows, b["sets"])):
            continue
        for k in range(len(rows) - 1, -1, -1):
            mf.tap(2047, set_rows(goto_tab(i, b["target"]))[k]["y"], pause=0.8)
        if any(not r["fields"] for r in set_rows(goto_tab(i, b["target"]))):
            raise Mismatch(f"{b['target']}: rows did not un-log")
        set_row_types(i, b)
        for k in range(len(rows)):
            mf.tap(2047, set_rows(goto_tab(i, b["target"]))[k]["y"], pause=0.8)
        if any(r["fields"] for r in set_rows(goto_tab(i, b["target"]))):
            raise Mismatch(f"{b['target']}: rows not all re-logged")
    mf.tap(2487, 115, pause=2.5)
    log("   fixed set types via editor")
    verify_saved(wid, already_open=True)


def repair_rir(blocks, fixes):
    """Rewrite reps/RIR of saved rows in the editor. Only the last logged row can be un-logged (tapping
    another row's box toggles 'skipped'), so un-log from the bottom up to the first bad row, re-enter the
    bad rows and re-log the rest in order. Un-logging keeps each row's values and RIR."""
    open_editor_from_detail()
    by_block = {}
    for i, k, have in fixes:
        by_block.setdefault(i, {})[k] = have
    for i, bad in by_block.items():
        b = blocks[i]
        nodes = goto_tab(i, b["target"])
        if headers(nodes)[-1] != "Reps":
            raise Mismatch(f"{b['target']}: last column is not Reps; cannot repair")
        n = len(b["sets"])
        if len(set_rows(nodes)) != n:
            raise Mismatch(f"{b['target']}: not all {n} rows visible for repair")
        for k in range(n - 1, min(bad) - 1, -1):
            mf.tap(2047, set_rows(dump())[k]["y"], pause=0.8)
        rows = set_rows(dump())
        if any(not rows[k]["fields"] for k in range(min(bad), n)):
            raise Mismatch(f"{b['target']}: rows did not un-log for repair")
        for k in range(min(bad), n):
            row = set_rows(goto_tab(i, b["target"]))[k]
            if k not in bad:
                mf.tap(2047, row["y"], pause=0.8)
                continue
            s, have = b["sets"][k], bad[k]
            want = None if s["rir"] is None else RIR_LABELS[min(int(s["rir"]), 6)]
            cmds = ["input text " + mf._quote(str(s["reps"])), "sleep 0.5"]
            if want != have:  # RIR selection survives un-logging; tap the shown value to clear it
                cmds.append(f"input tap {RIR_X[RIR_LABELS.index(want or have)]} {RIR_Y}; sleep 0.3")
            cmds.append(f"input tap {KEY_OK[0]} {KEY_OK[1]}")
            mf.tap_node(row["fields"][-1], pause=0.8)
            mf.sh("; ".join(cmds))
            time.sleep(1.0)
        rows = set_rows(goto_tab(i, b["target"]))
        if any(r["fields"] for r in rows):
            raise Mismatch(f"{b['target']}: rows not all logged after repair")
    mf.tap(2487, 115, pause=2.5)  # editor's save check mark
    log(f"   repaired reps/RIR on {len(fixes)} row(s) via editor")


# ---------------------------------------------------------------- main loop

COLCACHE_F = P("target-columns.json")
COLCACHE = json.load(open(COLCACHE_F, encoding="utf-8")) if os.path.exists(COLCACHE_F) else {}
BW_PARTS_F = P("bw-parts.json")  # per-exercise bodyweight share shown by the app, e.g. '+150.0 lb (95%)'
BW_PARTS = json.load(open(BW_PARTS_F, encoding="utf-8")) if os.path.exists(BW_PARTS_F) else {}


def apply_bw_part(nodes, b):
    """Assistance = app bodyweight share - felt load. The live workout chip disagrees with the
    share used in saved workouts, so use the share measured from verified saved detail (bw-parts.json)."""
    if not any(s.get("felt") is not None for s in b["sets"]):
        return
    bw = BW_PARTS.get(b["target"], CFG["app_bw"])
    for s in b["sets"]:
        if s.get("felt") is not None:
            s["lb"] = round(bw - s["felt"], 1)
            if s["lb"] <= 0:
                raise Mismatch(f"{b['target']}: felt {s['felt']} lb >= app bodyweight share {bw} lb")


def remember_columns(target, hs):
    if COLCACHE.get(target) != hs:
        COLCACHE[target] = hs
        json.dump(COLCACHE, open(COLCACHE_F, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


def import_one(wid):
    w = WORKOUTS[wid]
    blocks = plan(w)
    start, dur = start_end(w)
    log(f"== {wid} {w['date']} {w['name']!r} start={start} dur={dur} blocks={len(blocks)}")
    status = DB.execute("select status from workouts where source_id=?", (wid,)).fetchone()[0]
    if status == "entering":
        discard_importer_draft()
    try:
        to_workout_tab()
    except Mismatch:
        if status != "entering":
            raise
        discard_importer_draft()  # Back may have closed a chooser and exposed the draft it was hiding
        to_workout_tab()
    if status == "entering":  # interrupted earlier attempt: make sure it was not saved
        open_history()
        top = latest_imported()
        if top is not None and top.label.startswith(hist_date(work_date(w)) + "\n"):
            raise Mismatch(f"possible partial/duplicate entry already in history: {top.label!r}; resolve manually")
        mf.key(4, pause=1.0)
    set_status(wid, "entering")
    mf.tap(2472, 120, pause=1.0)
    mf.tap_label(r"^Start Empty Workout$", pause=2.0)
    # The tap lands ~1 s after the clock is set; keep the recorded start inside its source minute.
    mf.set_clock(start - timedelta(seconds=2) if start.second >= 57 else start)
    mf.tap_label(r"^Start Workout$", pause=2.0)
    chooser_add([b["target"] for b in blocks])
    for i, b in enumerate(blocks):
        enter_block(i, b)
    finish(start + timedelta(seconds=(dur or 60)))
    set_status(wid, "saved_unverified")
    log("   saved; verifying")
    verify_saved(wid, on_complete=True)


def fix_assisted(wid, bw=None):
    """Re-enter assisted-set loads of the latest imported workout via the workout editor, then re-verify.
    bw: measured app bodyweight share (saved-detail volume / reps) for the assisted targets, if it changed."""
    w = WORKOUTS[wid]
    blocks = plan(w)
    targets = [b for b in blocks if any(s.get("felt") is not None for s in b["sets"])]
    if bw is not None:
        for b in targets:
            BW_PARTS[b["target"]] = bw
        json.dump(BW_PARTS, open(BW_PARTS_F, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        blocks = plan(w)
    to_workout_tab()
    open_history()
    open_latest(w)
    for _ in range(4):
        e = [n for n in dump() if n.label and n.label.startswith("Edit Workout\n")]
        if e:
            break
        mf.swipe(1280, 1400, 1280, 700, ms=500, pause=1.0)
    mf.tap(1977, e[0].c[1], pause=2.5)
    if not has(dump(), r"^Edit Workout$"):
        raise Mismatch("workout editor did not open")
    for i, b in enumerate(blocks):
        if not any(s.get("felt") is not None for s in b["sets"]):
            continue
        for k in range(len(b["sets"]) - 1, -1, -1):  # un-log bottom-up; an earlier row's box can toggle "skipped"
            mf.tap(2047, set_rows(goto_tab(i, b["target"]))[k]["y"], pause=1.0)
        for k, s in enumerate(b["sets"]):
            row = set_rows(goto_tab(i, b["target"]))[k]
            if len(row["fields"]) != 2:
                raise Mismatch(f"{b['target']}: row {k + 1} not editable in editor")
            mf.tap_node(row["fields"][0], pause=0.8)
            mf.type_text(fmt_num(s["lb"]), pause=0.3)
            mf.tap(*KEY_OK, pause=0.9)
            mf.type_text(str(s["reps"]), pause=0.3)
            mf.tap(*KEY_OK, pause=1.2)
            if set_rows(goto_tab(i, b["target"]))[k]["fields"]:
                raise Mismatch(f"{b['target']}: row {k + 1} did not re-log in editor")
    mf.tap(2487, 115, pause=2.5)  # editor's save check mark
    log("   fixed assisted loads via editor")
    mf.key(4, pause=1.0)
    mf.key(4, pause=1.0)
    verify_saved(wid)


def verify_saved(wid, on_complete=False, already_open=False):
    """Read back the saved workout: on the post-finish Workout Complete screen, or via History."""
    w = WORKOUTS[wid]
    blocks = plan(w)
    for b in blocks:
        if b["target"] in COLCACHE:
            fit_columns(COLCACHE[b["target"]], b)
    if not on_complete:
        if not already_open:
            to_workout_tab()
            open_history()
            open_imported_workout(w)
        hdr = [n.label for n in dump() if n.c[1] < 160 and n.label]
        if hist_date(work_date(w)) not in hdr:
            raise Mismatch(f"detail header {hdr} != {hist_date(work_date(w))}")
    seq = collect_detail()
    if on_complete:  # clock is on the workout's own date, so its start shows as Today (Yesterday if past midnight)
        st = next((l for l in seq if l.startswith("Start Time\n")), "")
        if not re.match(r"Start Time\n(Today|Yesterday) ", st):
            raise Mismatch(f"start date not today/yesterday on device clock: {st!r}")
    RIR_FIXES.clear()
    problems = compare_detail(w, blocks, seq)
    if problems and RIR_FIXES and all(p.startswith("repairable ") for p in problems):
        log("   repairable mismatch:", "; ".join(problems))
        fixes = list(RIR_FIXES)
        repair_rir(blocks, fixes)
        if not on_complete and not has(dump(), "^" + re.escape(hist_date(work_date(w))) + "$"):
            raise Mismatch("not back on the workout detail after RIR repair")
        seq = collect_detail()
        RIR_FIXES.clear()
        problems = compare_detail(w, blocks, seq)
    detail = {"seq": seq}
    if not problems:
        try:
            if verify_rep_targets(blocks):
                detail["rep_targets_verified"] = True
        except Mismatch as ex:
            detail["rep_target_error"] = str(ex)
            set_status(wid, "saved_mismatch", detail)
            raise
    if problems:
        set_status(wid, "saved_mismatch", detail)
        log("   detail:", json.dumps(seq, ensure_ascii=False))
        raise Mismatch("; ".join(problems))
    set_status(wid, "verified", detail)
    log("   verified")
    if on_complete:
        mf.tap_label(r"^Done$", timeout=15, pause=1.5)
    else:
        mf.key(4, pause=1.0)
        mf.key(4, pause=1.0)


def main():
    a = sys.argv[1:]
    if a[0] == "plan":
        w = WORKOUTS[a[1]]
        print(json.dumps({"start": str(start_end(w)), "blocks": plan(w)}, indent=1, default=str))
        return
    if a[0] == "verify":
        try:
            verify_saved(a[1])
        except Exception as ex:
            log("VERIFY FAIL", a[1], type(ex).__name__, ex)
            sys.exit(2)
        return
    if a[0] == "fixnotes":
        try:
            fix_notes(a[1])
        except Exception as ex:
            log("FIX FAIL", a[1], type(ex).__name__, ex)
            sys.exit(2)
        return
    if a[0] == "fixreptargets":
        try:
            fix_pending_rep_targets()
        except Exception as ex:
            log("REP TARGET FIX FAIL", type(ex).__name__, ex)
            sys.exit(2)
        return
    if a[0] == "fix":
        try:
            fix_assisted(a[1], float(a[2]) if len(a) > 2 else None)
        except Exception as ex:
            log("FIX FAIL", a[1], type(ex).__name__, ex)
            sys.exit(2)
        return
    if a[0] == "run":
        n = int(a[1]) if len(a) > 1 and a[1].isdigit() else 1
        ids = a[a.index("--id") + 1:] if "--id" in a else None
        batch = pending(n, ids)
        unsynced = []
        try:
            for j, wid in enumerate(batch):
                run_one(wid)
                unsynced.append(wid)
                nxt = batch[j + 1] if j + 1 < len(batch) else None
                if nxt is None or WORKOUTS[nxt]["date"][:7] != WORKOUTS[wid]["date"][:7]:
                    sync_check(unsynced)  # once per calendar month, while the date jumps anyway
                    unsynced = []
        finally:
            if unsynced:  # stopped mid-month: still upload what was saved, at real time
                try:
                    sync_check(unsynced)
                except BaseException as ex:
                    log("final sync:", ex)
            else:
                mf.clock_auto()
        log("batch complete")


def run_one(wid):
    if os.path.exists(P("STOP")):
        log("STOP file present; halting before", wid)
        sys.exit(3)
    for attempt in (1, 2):
        try:
            import_one(wid)
            break
        except Exception as ex:
            st = DB.execute("select status from workouts where source_id=?", (wid,)).fetchone()[0]
            msg = str(ex)
            log("STOP" if attempt == 2 else "RETRY", wid, type(ex).__name__, msg)
            if attempt == 1 and st == "entering" and "duplicate" not in msg:
                continue
            new = {b["target"] for b in plan(WORKOUTS[wid]) if any(x.get("felt") is not None for x in b["sets"])}
            if (attempt == 1 and st == "saved_mismatch" and new  # share follows the app's dated bodyweight, so it can change
                    and new <= set(MEASURED_BW) and len({MEASURED_BW[t] for t in new}) == 1
                    and any("app bodyweight" in p for p in msg.split("; "))):
                try:  # adopt the app's measured share for this date and fix in place
                    mf.tap_label(r"^Done$", timeout=15, pause=1.5)
                    fix_assisted(wid, MEASURED_BW[next(iter(new))])
                    break
                except Exception as ex2:
                    log("STOP", wid, "auto-fix", type(ex2).__name__, ex2)
            if attempt == 1 and st == "saved_mismatch" and all(": rows " in p for p in msg.split("; ")):
                try:  # possibly set-type slips only; fix_types changes only types, then re-verifies
                    mf.tap_label(r"^Done$", timeout=15, pause=1.5)
                    to_workout_tab()
                    open_history()
                    open_imported_workout(WORKOUTS[wid])
                    fix_types(wid)
                    break
                except Exception as ex2:
                    log("STOP", wid, "type fix", type(ex2).__name__, ex2)
            if attempt == 1 and st == "saved_mismatch" and all(": note " in p for p in msg.split("; ")):
                try:  # entry error in note text only: rewrite the notes and re-verify
                    mf.tap_label(r"^Done$", timeout=15, pause=1.5)
                    fix_notes(wid)
                    break
                except Exception as ex2:
                    log("STOP", wid, "note fix", type(ex2).__name__, ex2)
            if attempt == 1 and st == "saved_unverified":  # saved, but reading it back failed (navigation)
                try:
                    verify_saved(wid)
                    break
                except Exception as ex2:
                    log("STOP", wid, "re-verify", type(ex2).__name__, ex2)
            try:
                print(mf.screenshot())
            except Exception:
                pass
            sys.exit(2)


if __name__ == "__main__":
    main()

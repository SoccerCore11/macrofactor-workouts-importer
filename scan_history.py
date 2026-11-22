"""Scroll the whole Workout History on the tablet and compare per-date workout counts with the source."""
import json, re, sys, time
from collections import Counter
from datetime import datetime
import importer as I, mf

DATE = re.compile(r"^(\w{3} \d+, \d{4})(\n|$)")

mf.tap(80, 640, pause=2)          # side rail: Workout
I.open_history()
time.sleep(1.5)

seq = []                          # ordered (date, card label)
prev_sig, same = None, 0
cur_date = None
for step in range(600):
    nodes = I.dump()
    view = []
    for n in sorted(nodes, key=lambda n: n.c[1]):
        if not n.label or n.c[1] < 170:
            continue
        m = DATE.match(n.label)
        if m and m.group(2) == "":            # group header for a date with several workouts
            view.append(("H", n.label, n.label))
        elif m:
            view.append(("C", m.group(1), n.label))
        elif n.clickable and "\n" in n.label:  # card inside a date group
            view.append(("G", None, n.label))
    sig = [v[2] for v in view]
    # merge: largest k where the tail of seq equals the head of this view
    tail = [s[2] for s in seq]
    k = 0
    for kk in range(min(len(tail), len(sig)), 0, -1):
        if tail[-kk:] == sig[:kk]:
            k = kk
            break
    seq.extend(view[k:])
    if sig == prev_sig:
        same += 1
        if same >= 2:
            break
    else:
        same = 0
    prev_sig = sig
    mf.drag(1280, 1350, 350, ms=600, pause=0.4)

dates = Counter()
cur = None
for kind, d, label in seq:
    if kind == "H":
        cur = d
    elif kind == "C":
        dates[d] += 1
    elif kind == "G" and cur:
        dates[cur] += 1

exp = Counter()
for wid, w in I.WORKOUTS.items():
    exp[I.hist_date(I.work_date(w))] += 1

def key(d):
    return datetime.strptime(d, "%b %d, %Y")

anchor = key(I.ANCHOR) if I.ANCHOR else datetime.max
res = {"tablet_total": sum(dates.values()), "source_total": sum(exp.values()),
       "missing": {d: (exp[d], dates.get(d, 0)) for d in exp if dates.get(d, 0) < exp[d]},
       "extra_before_anchor": {d: (exp.get(d, 0), c) for d, c in dates.items() if key(d) < anchor and c > exp.get(d, 0)},
       "tablet_from_anchor": sum(c for d, c in dates.items() if key(d) >= anchor),
       "tablet_before_anchor": sum(c for d, c in dates.items() if key(d) < anchor),
       "oldest": min(dates, key=key) if dates else None}
json.dump({"result": res, "seq": seq}, open(I.P("history-scan.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(json.dumps(res, indent=1))

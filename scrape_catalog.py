"""Scrape the full exercise catalog from an open, filter-cleared exercise chooser."""
import json
import os
import re
import sys
import time

import mf

DATA = os.environ.get("MF_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA, exist_ok=True)
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DATA, "catalog.json")
MUSCLES = ("Abs", "Adductors", "Biceps", "Calves", "Chest", "Forearms", "Front Delts", "Glutes",
           "Hamstrings", "Lats", "Lower Back", "Obliques", "Quads", "Rear Delts", "Serratus",
           "Side Delts", "Triceps", "Upper Back", "Upper Traps", "Abductors", "Neck", "Hip Flexors",
           "Tibialis", "Lower Traps", "Rotator Cuff")


def parse_item(label):
    parts = label.split("\n")
    if len(parts) < 2:
        return None
    musc = parts[-1]
    if not any(m in musc for m in MUSCLES) and "•" not in musc:
        return None
    name = parts[-2]
    return name, musc


import os
catalog = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
category = None
expanded = False
stale = 0
seen_more = set()
for i in range(600):
    nodes = mf.dump()
    new = 0
    for n in nodes:
        lab = n.label
        if not lab:
            continue
        m = re.match(r"^(.*)\n(See \d+ More|See Less)$", lab)
        if m:
            category = m.group(1)
            if m.group(2).startswith("See ") and "More" in m.group(2) and 300 < n.c[1] < 1450:
                if lab not in seen_more:
                    seen_more.add(lab)
                    mf.tap(2010, n.c[1], pause=1.0)
                    expanded = True
                    break
            continue
        if n.cls in ("ImageView", "View") and n.clickable and 400 < n.c[1] < 1560:
            p = parse_item(lab)
            if p and p[0] not in catalog:
                catalog[p[0]] = {"muscles": p[1], "category": category}
                new += 1
    if expanded:
        expanded = False
        continue  # re-dump without scrolling so rows just below the header are captured
    sig = tuple(n.label for n in nodes if n.label)
    stale = stale + 1 if sig == globals().get("_last_sig") else 0
    globals()["_last_sig"] = sig
    if stale >= 2:
        break
    if i % 10 == 0:
        print(f"iter {i} catalog={len(catalog)}", flush=True)
        json.dump(catalog, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    mf.swipe(1280, 1250, 1280, 700, ms=900, pause=0.8)

json.dump(catalog, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(f"done catalog={len(catalog)}")

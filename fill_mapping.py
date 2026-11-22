"""Fill empty targets in data/mapping.json with exact (case-insensitive) catalog matches and list the rest.

usage: python fill_mapping.py

Needs data/mapping.json (from prepare_workouts.py) and data/catalog.json (from scrape_catalog.py).
Only exact name matches are filled; everything else must be mapped by hand.
"""
import json
import os

DATA = os.environ.get("MF_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
mapping_f, catalog_f = os.path.join(DATA, "mapping.json"), os.path.join(DATA, "catalog.json")
mapping = json.load(open(mapping_f, encoding="utf-8"))
catalog = {name.lower(): name for name in json.load(open(catalog_f, encoding="utf-8"))}

filled, todo = [], []
for src, m in mapping.items():
    if m.get("target"):
        if m["target"] not in catalog.values():
            print(f"WARNING: {src!r} -> {m['target']!r} is not in the catalog")
        continue
    hit = catalog.get(src.lower())
    if hit:
        m.update(target=hit, confidence="exact")
        filled.append(src)
    else:
        todo.append(src)
json.dump(mapping, open(mapping_f, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print(f"filled {len(filled)} exact matches; {len(todo)} still need a target:")
for src in todo:
    print("  ", src)

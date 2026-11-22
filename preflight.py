"""Dry-run every pending workout through the planner (no device access) and summarise targets and errors."""
import json, sqlite3, collections, importer as I
db=sqlite3.connect(I.P('progress.sqlite3'))
rem=[r[0] for r in db.execute("select source_id from workouts where status!='verified' and status not like 'already%' order by date")]
cat=json.load(open(I.P('catalog.json'),encoding='utf-8'))
errs=collections.defaultdict(list); tgt=collections.defaultdict(lambda: {'n':0,'kinds':collections.Counter(),'first':None,'srcs':set()})
assisted=collections.Counter()
for wid in rem:
    w=I.WORKOUTS[wid]
    try: bl=I.plan(w)
    except Exception as e: errs[str(e)[:150]].append(wid); continue
    for b in bl:
        t=tgt[b['target']]; t['n']+=1; t['first']=t['first'] or wid; t['srcs'].add(b['source_exercise'])
        for s in b['sets']:
            t['kinds'][tuple(k for k in ('lb','reps','seconds','dist') if s[k] is not None)]+=1
            if s.get('felt') is not None: assisted[b['target']]+=1
print('remaining',len(rem))
print('PLAN ERRORS'); [print(' ',k,len(v),v[:3]) for k,v in errs.items()]
print('ASSISTED w/o BW part',[k for k in assisted if k not in I.BW_PARTS])
print('TARGETS')
for k,t in sorted(tgt.items(), key=lambda x:-x[1]['n']):
    cols=I.COLCACHE.get(k)
    print(json.dumps([k, cols, cat.get(k,{}).get('category'), t['n'], dict((','.join(a),c) for a,c in t['kinds'].items()), sorted(t['srcs']), t['first']],ensure_ascii=False))


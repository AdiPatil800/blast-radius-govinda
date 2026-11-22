import marshal,glob,collections,sys
tot={}
for f in glob.glob(sys.argv[1]+'/probe_gw*.bin'):
    d=marshal.load(open(f,'rb'))
    for k,v in d.items(): tot[k]=tot.get(k,0)|v
print('nodes',len(tot),'executed',sum(1 for v in tot.values() if v&1))
for k in [('sqlglot/dialects/dialect.py',1280),('sqlglot/executor/context.py',53),('sqlglot/generators/clickhouse.py',811),('sqlglot/generators/clickhouse.py',55)]:
    print(k,[(kk,bin(v)) for kk,v in tot.items() if kk[:2]==k])
c=collections.Counter()
for v in tot.values():
    if v&1:
        for b,n in ((2,'is/=='),(4,'isnot/!='),(8,'</<='),(16,'>/>=')): c[n+(' differ' if v&b else ' same')]+=1
print(c)
if len(sys.argv)>2: marshal.dump(tot, open(sys.argv[2],'wb'))

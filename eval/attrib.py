"""Which tests account for the excess: marginal cost of each wrongly selected test."""
import json,sys,collections
data,out=sys.argv[1],sys.argv[2]
T={t['number']:t for t in json.load(open(data+'/tests.json'))['tests']}
A=json.load(open(data+'/answers.json'))['commits']
ms={k:round(v['seconds']*1000) for k,v in T.items()}
def pack_cost(tests):
    if not tests: return 0.0
    order=sorted(tests,key=lambda t:-ms[t]); best=None
    for k in range(1,min(8,len(order))+1):
        loads=[0]*k
        for t in order:
            i=loads.index(min(loads)); loads[i]+=ms[t]
        c=max(loads)+2000*k
        if best is None or c<best: best=c
        if max(loads)<=ms[order[0]]: break
    return best/1000
marg=collections.Counter(); cnt=collections.Counter(); tot=0; group=collections.Counter(); n=0; allpass=0; allpass_n=0
for line in open(out):
    o=json.loads(line); n+=1; sel={t for g in o['workers'] for t in g}; f=set(A[o['commit']]['failing'])
    if len(sel)>1200: continue
    base=pack_cost(sel); ex=base-pack_cost(f&sel); tot+=ex
    if not f and sel: allpass+=ex; allpass_n+=1
    fp=sel-f
    for t in sorted(fp,key=lambda t:-ms[t])[:12]:
        m=base-pack_cost(sel-{t})
        if m>0.05: marg[T[t]['id'].split('::',1)[1]]+=m; cnt[T[t]['id'].split('::',1)[1]]+=1
    for name,pred in (('TestOptimizer*',lambda i:'test_optimizer.py' in i),('TestExecutor*',lambda i:'test_executor.py' in i),('dialects',lambda i:'/dialects/' in i),('other',lambda i:not any(x in i for x in ('test_optimizer.py','test_executor.py','/dialects/')))):
        rm={t for t in fp if pred(T[t]['id'])}
        group[name]+=base-pack_cost(sel-rm)
print('n=%d excess per30 %.1f (commits where nothing fails: %d, %.1f per30)'%(n,tot/n*30,allpass_n,allpass/n*30))
print('removing all wrong picks of a group saves per30:',{k:round(v/n*30,1) for k,v in group.items()})
for k,v in marg.most_common(int(sys.argv[3]) if len(sys.argv)>3 else 10): print('  %-52s %.1f s over %d commits (%.2f per30)'%(k,v,cnt[k],v/n*30))

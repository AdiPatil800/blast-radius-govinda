"""Merge unit digests taken under several hash seeds into maps/head.bin.

    python tools/pack_digests.py MAPS_DIR DIGESTS.bin [DIGESTS.bin ...]

An item keeps its digests only when all runs agree; otherwise it has none and
the selector never settles it by comparison.
"""
import marshal
import os
import sys

runs = [marshal.load(open(p, "rb")) for p in sys.argv[2:]]
merged = {}
for t in runs[0]:
    n = len(runs[0][t])
    merged[t] = [
        runs[0][t][i] if all(len(r.get(t, [])) == n and r[t][i] == runs[0][t][i] and "?" not in r[t][i] for r in runs) else None
        for i in range(n)
    ]
    print(t.split("::")[-1], n, "items,", sum(x is None for x in merged[t]), "unstable")
path = os.path.join(sys.argv[1], "head.bin")
head = marshal.load(open(path, "rb"))
head["unit_fp"] = merged
marshal.dump(head, open(path, "wb"))

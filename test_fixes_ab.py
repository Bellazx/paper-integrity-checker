import numpy as np, sys, importlib
sys.path.insert(0, ".")
import utils.stats as st
importlib.reload(st)

# Fix A: a 4501-pt curve with ~132 unique -> should NOT be high (medium at most)
arr = np.tile(np.round(np.linspace(0, 1, 132), 4), 35)[:4501]
vr = st.check_value_recycling(arr)
print("A curve(ratio=%.3f) severity=%s  EXPECT medium" % (vr.get("ratio", -1), vr.get("severity")))

# Fix A: small fabricated table 12 pts, 3 unique -> still high
vr2 = st.check_value_recycling(np.array([1.,1.,1.,2.,2.,2.,3.,3.,3.,1.,2.,3.]))
print("A small(ratio=%.3f) severity=%s  EXPECT high" % (vr2.get("ratio", -1), vr2.get("severity")))

# Fix B: two different Y curves sharing X -> not flagged
x = np.arange(0, 50, 1.0)
r = st.check_cross_group_duplicates({"a": np.sin(x), "b": np.cos(x)})
print("B diff-Y flagged=%d  EXPECT 0" % len(r))

# Fix B: exact copy -> high
ya = np.sin(x)
r2 = st.check_cross_group_duplicates({"c1": ya, "c2": ya.copy()})
print("B exact-copy sev=%s  EXPECT ['high']" % [z["severity"] for z in r2])

# Fix B: shared arithmetic X axis -> skipped
r3 = st.check_cross_group_duplicates({"x1": x, "x2": x.copy()})
print("B shared-axis flagged=%d  EXPECT 0" % len(r3))

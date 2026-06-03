import numpy as np, sys, importlib
sys.path.insert(0, ".")
import utils.stats as st; importlib.reload(st)
import modules.data_checker as dc; importlib.reload(dc)

# Full-pipeline value_recycling via _analyze_column_group (includes data_checker downgrade guard)
# 1) Real spectrum/curve: 4501 pts, 132 unique -> should end up MEDIUM (not high)
curve = np.tile(np.round(np.linspace(0, 1, 132), 4), 35)[:4501]
an = dc._analyze_column_group(curve, "f/Fig1/Intensity", col_name="Intensity (a.u.)")
vr = [a for a in an if a["test"] == "value_recycling"]
print("curve value_recycling:", [(a["severity"]) for a in vr], " EXPECT [] or ['medium']")

# 2) Fabricated small-ish float table: 60 pts, 6 unique non-integer values, ratio 0.1 -> HIGH
fab = np.tile(np.array([1.11, 2.22, 3.33, 4.44, 5.55, 6.66]), 10)
an2 = dc._analyze_column_group(fab, "f/T/x", col_name="Expression level")
vr2 = [a for a in an2 if a["test"] == "value_recycling"]
print("fab small value_recycling:", [(a["severity"]) for a in vr2], " EXPECT ['high']")

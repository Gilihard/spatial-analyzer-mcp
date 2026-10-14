"""Run every offline regression check and summarise the result."""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

checks = sorted(f for f in os.listdir(HERE)
                if f.startswith("check_") and f.endswith(".py"))

# CI runners have no interactive desktop session for the MessageBox test.
SKIP = {n.strip() for n in
        os.environ.get("SAMCP_SKIP_CHECKS", "").replace(",", " ").split()
        if n.strip()}
skipped = [c for c in checks if c in SKIP]
checks = [c for c in checks if c not in SKIP]
if skipped:
    print("skipped (SAMCP_SKIP_CHECKS): " + ", ".join(skipped), flush=True)

env = dict(os.environ, PYTHONPATH=ROOT)
failed = []
for name in checks:
    print(f"== {name}", flush=True)
    proc = subprocess.run([sys.executable, os.path.join(HERE, name)],
                          cwd=ROOT, env=env)
    if proc.returncode:
        failed.append(name)

print()
print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED: " + ", ".join(failed))
raise SystemExit(1 if failed else 0)

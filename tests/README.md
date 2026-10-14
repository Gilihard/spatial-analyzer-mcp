# Offline regression checks

Self-checking scripts that pin the contracts this server depends on:
the SA step / argument names, the COM argument transport (InvokeTypes,
VARIANT layouts), the ref-list layout, the fit math and the dialog
watchdog logic.

They need **no** SpatialAnalyzer, no COM engine and no MCP client — every
one of them forces the offline path itself (a `no_sa` stub or a fake
bridge), so they stay deterministic even while a live SA is running.
Windows-only, because they import the same modules the server does
(`pywin32`).

Run one of them:

```bat
python tests/check_fitmath.py      :: one check
python tests/run_all.py            :: all of them, one summary line
```

`check_dialogs.py` pops real MessageBoxes, so it needs an interactive desktop
session; skip it with `set SAMCP_SKIP_CHECKS=check_dialogs.py` (that is what
CI does).

| Script | What it pins |
|---|---|
| `check_dialogs.py` | Modal-dialog dismissal (real MessageBoxes owned by the test process). |
| `check_openlogic.py` | Watchdog gate + attach-first open logic. |
| `check_identify.py` | Offline shape classification. |
| `check_frames_delete.py` | Delete/frame logic, `Construct Frame` arg setter, ordering, active collection. |
| `check_fitmath.py` | Constrained (fixed) fit math. |
| `check_cardinal_logic.py` | Auto cardinal-point group purge (fake bridge). |
| `check_move.py` | Transform matrix transport + move-mode validation. |
| `check_average.py` | Group-averaging arg names / setter types / ordering. |
| `check_unify.py` | USMN arg contract + retry, exclusion derivation, fit pipeline ordering, temp cleanup. |
| `check_fit_transform.py` | `sa_best_fit_transform` arg/DOF contract, deviation math, worst-first sort, exclude -> temp copy -> refit, apply + `stats_after_move`. |

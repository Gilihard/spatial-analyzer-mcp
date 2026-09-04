# -*- coding: utf-8 -*-
"""Live end-to-end check of the view-control (show/hide) tools vs SA 2015.

Confirms the step + arg names of "Show Objects" / "Hide Objects" /
"Show/Hide Points" / "Show/Hide by Object Type" against a real SA instance
and the joined-full-name ref-list layout they consume (the same names
sa_inspect_project returns). Every hide is followed by the matching show, so
the job's graphics state is restored at the end. Run with SA closed (this
cold-starts SA):  python _live_showhide.py
"""
import os
import sys
import threading
import time

import sa_app
import sa_sdk
import server

# Cap every COM call at 12 s so a wedged SA costs seconds, not a minute.
_orig_submit = sa_sdk.SABridge._submit


def _fast_submit(self, func, *args, timeout=60.0, **kwargs):
    return _orig_submit(self, func, *args, timeout=12.0, **kwargs)


sa_sdk.SABridge._submit = _fast_submit

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "6.01.25 — обработка.xit")
FAILURES = []


def t(label):
    print(f"[{time.monotonic():8.1f}] {label}", flush=True)


def dismiss(pid=None):
    try:
        return sa_app._dismiss_modal_dialogs(pid) or []
    except Exception:  # noqa: BLE001
        return []


_seen_nags = set()


def _nag_watchdog(pid, stop):
    """Close modal dialogs on the launched SA every 2 s; log any titles."""
    while not stop.is_set():
        for x in dismiss(pid):
            if x not in _seen_nags:
                _seen_nags.add(x)
                t(f"NAG-DIALOG dismissed: '{x}'")
        stop.wait(2.0)


def settle(sec, pid=None):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        time.sleep(2.0)
        dismiss(pid)


def check(label, cond, extra=""):
    print(f"    CHECK {label}: {'PASS' if cond else 'FAIL'} {extra}",
          flush=True)
    if not cond:
        FAILURES.append(label)


def main():
    t("ensure SA (cold start if needed)")
    run = server.sa_ensure_running()
    t(f"ensure: running={run.get('running')} launched={run.get('launched')} "
      f"connected={run.get('connected')} err={run.get('error')}")
    if not run.get("connected"):
        t("ABORT: bridge not connected")
        sys.exit(1)
    pids = sa_app.sa_gui_pids()
    pid = pids[0] if pids else None
    stop = threading.Event()
    if pid is not None:
        threading.Thread(target=_nag_watchdog, args=(pid, stop),
                         daemon=True).start()
    if run.get("launched"):
        t(f"wait 45 s post-launch dismissing dialogs on pid={pid}")
        settle(45, pid)
    else:
        settle(4, pid)
    t(f"nags seen: {sorted(_seen_nags) or 'none'}")

    t(f"open fixture {os.path.basename(FIXTURE)}")
    op = server.sa_open_file(FIXTURE)
    t(f"open: {op.get('opened')} status={op.get('status')} "
      f"err={op.get('error')}")
    if not op.get("opened"):
        t("ABORT: could not open fixture")
        sys.exit(1)
    settle(3, pid)

    # --- 1) objects: hide / show a real point group by full name -----------
    groups = server.sa_inspect_project(
        collection="A", object_types=["Point Group"]).get("types", {}).get(
        "Point Group", [])
    groups = [str(g) for g in (groups or [])]
    t(f"groups in A: {groups}")
    if not groups:
        t("ABORT: no point group in collection A")
        sys.exit(1)
    g_full = groups[0]
    g_bare = g_full.split("::", 1)[-1]

    t(f"hide object '{g_full}'")
    r = server.sa_hide_objects([g_full])
    print(f"    -> {r.get('hidden')} step={r.get('step')} "
          f"code={r.get('status_code')} {r.get('status')} "
          f"err={r.get('error')}", flush=True)
    check("hide_objects SUCCESS", r.get("hidden")
          and r.get("status_code") == 2 and not r.get("error"))
    check("hide_objects normalizes name", r.get("objects") == [g_full])

    t(f"show object '{g_full}' back")
    r = server.sa_show_objects([g_full])
    print(f"    -> {r.get('shown')} code={r.get('status_code')} "
          f"{r.get('status')} err={r.get('error')}", flush=True)
    check("show_objects SUCCESS", r.get("shown")
          and r.get("status_code") == 2 and not r.get("error"))

    # simple name + collection resolution
    t(f"hide by simple name '{g_bare}' + collection 'A'")
    r = server.sa_hide_objects([g_bare], collection="A")
    check("hide_objects simple+collection resolves",
          r.get("objects") == [g_full] and r.get("hidden")
          and not r.get("error"))
    server.sa_show_objects([g_bare], collection="A")

    # --- 2) PARTIAL SUCCESS when one name does not exist -------------------
    t("hide [real group, bogus name] -> expect PARTIAL SUCCESS (code 4)")
    r = server.sa_hide_objects([g_full, "A::MCP_BOGUS_DOES_NOT_EXIST"])
    print(f"    -> hidden={r.get('hidden')} code={r.get('status_code')} "
          f"{r.get('status')} msgs={r.get('messages')}", flush=True)
    check("hide_objects PARTIAL (bogus name, code 4)",
          r.get("hidden") and r.get("status_code") == 4
          and not r.get("error"))
    server.sa_show_objects([g_full])

    # --- 3) points: hide/show a few points of a small group ----------------
    by_count = []
    for g in groups:
        pts = server._points_in_group(g)
        if len(pts) >= 3:
            by_count.append((g, pts))
    if not by_count:
        t("ABORT: no group with >= 3 points")
        sys.exit(1)
    small = min(by_count, key=lambda x: len(x[1]))
    sg_full, sg_pts = small
    targets = sg_pts[:3]
    t(f"hide 3 points of '{sg_full}': {targets}")
    r = server.sa_hide_points(targets, collection="A")
    print(f"    -> hidden={r.get('hidden')} code={r.get('status_code')} "
          f"{r.get('status')} points={r.get('points')} err={r.get('error')}",
          flush=True)
    check("hide_points SUCCESS + relative names joined",
          r.get("hidden") and r.get("status_code") == 2 and not r.get("error")
          and r.get("points") == ["A::" + p if not p.startswith("::")
                                  else p for p in targets])
    r = server.sa_show_points(targets, collection="A")
    print(f"    -> shown={r.get('shown')} code={r.get('status_code')} "
          f"{r.get('status')}", flush=True)
    check("show_points SUCCESS", r.get("shown")
          and r.get("status_code") == 2 and not r.get("error"))

    # bare target + group param
    t(f"hide point '1' of group '{sg_full}' via bare name")
    r = server.sa_hide_points(["1"], group=sg_full, collection="A")
    check("hide_points bare target + group resolves",
          r.get("hidden") and not r.get("error")
          and r.get("points") == ["A::" + sg_full.split("::", 1)[-1] + "::1"])
    server.sa_show_points(["1"], group=sg_full, collection="A")

    # --- 4) Show/Hide by Object Type ---------------------------------------
    for ot in ("Frame", "Point Group"):
        t(f"hide all '{ot}' in collection A, then show again")
        r = server.sa_show_hide_by_type(ot, visible=False, collection="A")
        print(f"    hide -> applied={r.get('applied')} "
              f"code={r.get('status_code')} {r.get('status')} "
              f"err={r.get('error')}", flush=True)
        check(f"by_type hide {ot} applied",
              r.get("applied") and r.get("status_code") == 2
              and not r.get("error"))
        r = server.sa_show_hide_by_type(ot, visible=True, collection="A")
        print(f"    show -> applied={r.get('applied')} "
              f"code={r.get('status_code')}", flush=True)
        check(f"by_type show {ot} applied",
              r.get("applied") and r.get("status_code") == 2
              and not r.get("error"))

    t("hide all 'Frame' across ALL collections, then show again")
    r = server.sa_show_hide_by_type("Frame", visible=False,
                                    all_collections=True)
    print(f"    hide -> applied={r.get('applied')} "
          f"code={r.get('status_code')}", flush=True)
    check("by_type all_collections hide applied",
          r.get("applied") and r.get("status_code") == 2 and not r.get("error"))
    r = server.sa_show_hide_by_type("Frame", visible=True, all_collections=True)
    check("by_type all_collections show applied",
          r.get("applied") and r.get("status_code") == 2 and not r.get("error"))

    settle(2, pid)
    stop.set()
    t("DONE")
    print("FAILED: " + "; ".join(FAILURES) if FAILURES else "ALL OK",
          flush=True)
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()

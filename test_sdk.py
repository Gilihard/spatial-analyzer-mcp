"""Standalone smoke test for the SA COM bridge (NOT the MCP server).

Run from the project folder:
    python test_sdk.py

Checks, in order:
  1. The SpatialAnalyzerSDK COM ProgID is registered.
  2. The bridge worker thread starts and creates the COM object.
  3. (Optional) Connect to a running SA instance and build a test point.

SpatialAnalyzer does NOT need to be running for checks 1-2. For check 3,
open SA first (Utilities > SDK settings must allow connections) and pass the
host as an argument:  python test_sdk.py localhost
"""

import sys

from sa_sdk import SA_PROG_ID, SAError, SABridge


def line(msg):
    print(f"  {msg}")


def check(label, fn):
    try:
        fn()
        line(f"[OK]   {label}")
        return True
    except Exception as exc:  # noqa: BLE001
        line(f"[FAIL] {label}: {exc}")
        return False


def main():
    print("SA SDK COM bridge - smoke test")
    print("-" * 50)

    bridge = None

    def make_bridge():
        nonlocal bridge
        bridge = SABridge()

    if not check("Dispatch COM object (ProgID registered)", make_bridge):
        line(f"\nMake sure SpatialAnalyzer is installed. Expected ProgID:")
        line(f"  {SA_PROG_ID}")
        sys.exit(1)

    def assert_not_connected():
        if bridge.is_connected():
            raise AssertionError("bridge says connected before Connect()")
    check("Bridge reports not connected yet", assert_not_connected)

    host = sys.argv[1] if len(sys.argv) > 1 else None
    if host:
        print(f"\nAttempting to connect to SA on '{host}'...")
        if check("Connect()", lambda: bridge.connect(host) or
                  (_ for _ in ()).throw(AssertionError("Connect() False"))):
            line(f"Connected! host={bridge.host}")

            print("\nBuilding a test point (group=TestGrp, name=McpTestPt)...")
            try:
                bridge.set_step("Construct a Point in Working Coordinates")
                bridge.set_vector_arg("Working Coordinates", 10.0, 20.0, 30.0)
                bridge.set_point_name_arg("Point Name", "", "TestGrp", "McpTestPt")
                ok = bridge.execute_step()
                code = bridge.get_step_result()
                line(f"ExecuteStep -> {ok}, result code {code}")
            except Exception as exc:  # noqa: BLE001
                line(f"Point construction error (SA step name may differ): {exc}")
        else:
            line("Is SpatialAnalyzer running with SDK connections enabled?")
    else:
        line("\nNo host given - skipping live connect test.")
        line("To test a live connection:  python test_sdk.py localhost")

    print("-" * 50)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except SAError as exc:
        print(f"\nSAError: {exc}")
        sys.exit(2)

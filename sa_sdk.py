"""Bridge to the Spatial Analyzer COM SDK.

SpatialAnalyzer exposes its automation API through a COM object
(SpatialAnalyzerSDK.SpatialAnalyzerSDKClass). COM objects are tied to the
thread that created them, so we run ALL COM calls on a single dedicated
worker thread. The MCP server's async tools submit work to this thread via a
queue and wait for the result. This keeps the COM apartment stable no matter
which asyncio task calls a tool.
"""

import atexit
import queue
import threading
import uuid

import pythoncom
from win32com.client import Dispatch

# ProgID of the SA SDK COM server. Registered when SpatialAnalyzer is installed.
# Note: the .NET examples reference "SpatialAnalyzerSDKClass", but the COM
# ProgID actually registered in the registry is "SpatialAnalyzerSDK.Application".
SA_PROG_ID = "SpatialAnalyzerSDK.Application"

# All ISpatialAnalyzerSDK method names. We flag these on the Dispatch object so
# pywin32 treats them as methods (not properties). Without this, parameterless
# methods like ExecuteStep()/GetMPStepResult() get *invoked* on attribute access
# and return their result instead of a callable, raising
# "'bool' object is not callable".
SA_METHODS = (
    "Connect", "ConnectEx", "SetStep", "ExecuteStep", "GetMPStepResult",
    "GetMPStepMessages", "SetPointNameArg", "GetPointNameArg", "SetVectorArg",
    "GetVectorArg", "SetCollectionObjectNameArg", "GetCollectionObjectNameArg",
    "SetDoubleArg", "GetDoubleArg", "SetIntegerArg", "GetIntegerArg",
    "SetBoolArg", "GetBoolArg", "SetStringArg", "GetStringArg",
    "SetObjectNameArg", "GetObjectNameArg", "SetTransformArg", "GetTransformArg",
    "SetInstIdArg", "GetInstIdArg", "SetColInstIdArg", "GetColInstIdArg",
    "SetFilePathArg", "GetFilePathArg",
    "SetCollectionObjectNameRefListArg", "GetCollectionObjectNameRefListArg",
    "SetFrameNameArg", "GetFrameNameArg", "SetCollectionNameArg",
    "GetCollectionNameArg", "SetVectorGroupNameArg", "GetVectorGroupNameArg",
    "SetCloudNameArg", "GetCloudNameArg", "SetPerimeterNameArg",
    "GetPerimeterNameArg", "SetChartNameArg", "GetChartNameArg",
    "SetPointNameRefListArg", "GetPointNameRefListArg",
    "SetVectorNameRefListArg", "GetVectorNameRefListArg",
    "SetCollectionVectorGroupNameRefListArg",
    "GetCollectionVectorGroupNameRefListArg",
    "SetWorldTransformArg", "GetWorldTransformArg", "SetInstTypeNameArg",
    "SetFontTypeArg", "SetGeometryTypeArg", "SetSAInteractionModeArg",
    "SetMPInteractionModeArg", "SetMPDialogInteractionModeArg",
    "SetWorkbookAddressModeTypeArg", "SetMoveDirectionTypeArg",
    "SetWriteModeTypeArg", "SetProjectionOptionsArg",
    "SetCollectionGroupNameRefListArg", "GetCollectionGroupNameRefListArg",
    "SetExportDataDelimeterTypeArg", "SetExportTargetNameFormatArg",
    "SetCoordinateSystemTypeArg", "SetAsciiFileFormatArg",
    "SetDistanceUnitsArg", "SetObjectTypeArg", "SetViewNameArg",
    "GetViewNameArg", "SetColVectorGroupNameArg", "GetColVectorGroupNameArg",
    "SetColInstIdRefListArg", "GetColInstIdRefListArg", "SetFitDofOptionsArg",
    "SetReportOutputOptionsArg", "GetReportOutputOptionsArg",
    "SetStringRefListArg", "GetStringRefListArg", "SetUserSummaryInfoFilesArg",
    "SetReportViewOptionsArg", "GetReportViewOptionsArg", "SetAxisNameArg",
    "SetReportTypeArg", "SetBaseColorTypeArg", "SetColorRangeMethodArg",
    "SetColorizationOptionsArg", "SetColorArg", "SetWindowStateArg",
    "SetResultArg", "GetResultArg", "SetRenderModeTypeArg",
    "SetSurfDissectModeTypeArg", "SetShowUsmnDialogTypeArg",
    "SetReportPageSettingsArg", "SetPointDeltaReportOptionsArg",
    "SetDatasetTypeArg", "SetChartTypeArg", "SetColMachineIdArg",
    "GetColMachineIdArg", "SetExportVectorNameFormatArg",
    "SetSystemStringArg", "SetCloudThinningOptionsArg",
    "SetToleranceScalarOptionsArg", "GetToleranceScalarOptionsArg",
    "SetOffsetDirectionTypeArg", "SetCollectionObjectNameArg2",
    "SetDoubleArrayArg", "GetDoubleArrayArg",
)

# Step result codes returned by GetMPStepResult().
MP_STATUS = {
    -1: "SdkError",
    0: "Undone",
    1: "InProgress",
    2: "DoneSuccess",
    3: "DoneFatalError",
    4: "DoneMinorError",
    5: "CurrentTask",
}


class SAError(RuntimeError):
    """Raised when an SA SDK call fails."""


def _unwrap(raw, cast):
    """Extract a single [out] value from a pywin32 Dispatch result.

    COM methods in the SA SDK return a BOOL plus one [out] value. Late binding
    may return: the value directly, a (bool, value) tuple, or just a bool when
    extraction failed. This helper normalises all three to `cast(value)`.
    """
    if isinstance(raw, tuple):
        for item in raw:
            if not isinstance(item, bool):
                try:
                    return cast(item)
                except (TypeError, ValueError):
                    continue
        # fallback: last element
        return cast(raw[-1])
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise SAError(f"Could not unwrap result {raw!r}")


class _Task:
    __slots__ = ("func", "args", "kwargs", "done", "result", "error")

    def __init__(self, func, args, kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.done = threading.Event()
        self.result = None
        self.error = None


class SABridge:
    """Owns the SA COM object on a private thread.

    Public methods are blocking; they post a task to the worker, wait for the
    result, and return it. Thread-safe by construction (single worker).
    """

    def __init__(self):
        self._q: "queue.Queue[_Task | None]" = queue.Queue()
        self._sdk = None  # set inside the worker thread
        self._init_error = None
        self._ready = threading.Event()
        self.host = None
        self.connected = False
        self._worker = threading.Thread(
            target=self._run, name="sa-com-worker", daemon=True
        )
        self._worker.start()
        # Wait until the worker has created the COM object (or failed).
        if not self._ready.wait(timeout=20):
            raise SAError("SA COM worker failed to start within 20s")
        if self._sdk is None:
            raise SAError(
                f"Could not create SA COM object (ProgID={SA_PROG_ID}): "
                f"{self._init_error}"
            )
        # The SDK COM server is out-of-process: unless we Release() it on the
        # owning thread, the engine process leaks (each run spawns a new one).
        atexit.register(self.shutdown)

    def shutdown(self):
        """Release the COM object and stop the worker thread.

        Each SABridge owns a separate SpatialAnalyzerSDK engine process. That
        engine only terminates when its last COM reference is released in the
        apartment that created it. We drop the reference on the worker thread,
        then let the worker exit so CoUninitialize() runs. Idempotent.
        """
        if self._worker.is_alive():
            def _release():
                # Drop all references in the creating apartment so the
                # out-of-proc engine sees refcount 0 and quits.
                self._sdk = None
            try:
                self._submit(_release, timeout=10)
            except Exception:  # noqa: BLE001 - best effort during teardown
                pass
            self._q.put(None)
            self._worker.join(timeout=5)
        self.connected = False

    # ------------------------------------------------------------------ worker
    def _run(self):
        pythoncom.CoInitialize()
        try:
            self._sdk = Dispatch(SA_PROG_ID)
            # Flag all SDK methods so pywin32 dynamic dispatch returns callables
            # rather than invoking parameterless methods on attribute access.
            for m in SA_METHODS:
                try:
                    self._sdk._FlagAsMethod(m)
                except Exception:  # noqa: BLE001 - ignore unknown members
                    pass
        except Exception as exc:  # pragma: no cover - environment dependent
            self._init_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                task = self._q.get()
                if task is None:
                    break
                try:
                    task.result = task.func(*task.args, **task.kwargs)
                except Exception as exc:  # noqa: BLE001 - surface any COM error
                    task.error = exc
                finally:
                    task.done.set()
        finally:
            pythoncom.CoUninitialize()

    def _submit(self, func, *args, timeout=60.0, **kwargs):
        task = _Task(func, args, kwargs)
        self._q.put(task)
        if not task.done.wait(timeout=timeout):  # blocking; MCP tool is sync
            raise SAError(
                f"SA COM call timed out after {timeout}s. The SA process may be "
                f"blocked (e.g. a modal dialog is open, or a previous client "
                f"left the SDK connection stuck). Restart SA and retry."
            )
        if task.error is not None:
            raise task.error
        return task.result

    # ---------------------------------------------------------- raw dispatch
    def _call(self, method, *args):
        """Call a COM method entirely on the worker thread (closure).

        Both the attribute lookup AND the call must happen on the worker,
        because the IDispatch lives in an STA created there. Touching it from
        the main thread would violate the COM apartment.
        """
        def _invoke():
            return getattr(self._sdk, method)(*args)
        return self._submit(_invoke)

    # ------------------------------------------------------------ public API
    def connect(self, host: str) -> bool:
        """Connect to a running SpatialAnalyzer instance by hostname/IP."""
        ok = self._submit(lambda: self._sdk.Connect(host))
        self.host = host
        self.connected = bool(ok)
        return bool(ok)

    def is_connected(self) -> bool:
        return self.connected

    # -- generic step -------------------------------------------------------
    def set_step(self, name: str):
        self._call("SetStep", name)

    def execute_step(self) -> bool:
        return bool(self._call("ExecuteStep"))

    def get_step_result(self) -> int:
        # GetMPStepResult([out] long* code, [out,retval] BOOL). The SA COM
        # object exposes NO type library (GetTypeInfoCount==0), so pywin32
        # dynamic dispatch cannot return the [out] value -- it raises
        # DISP_E_PARAMNOTOPTIONAL. We bypass dynamic dispatch and call
        # IDispatch::Invoke directly with an explicit [out] type descriptor.
        # InvokeTypes returns (retval_BOOL, out_long); we keep the code.
        def _invoke():
            ole = self._sdk._oleobj_
            dispid = self._dispid("GetMPStepResult")
            res = ole.InvokeTypes(
                dispid, 0, pythoncom.DISPATCH_METHOD,
                (pythoncom.VT_BOOL, 0),
                ((pythoncom.VT_I4 | pythoncom.VT_BYREF, 0),),
                0,
            )
            return int(res[-1] if isinstance(res, tuple) else res)
        return self._submit(_invoke)

    def _dispid(self, name: str) -> int:
        cache = getattr(self, "_dispid_cache", None)
        if cache is None:
            cache = self._dispid_cache = {}
        if name not in cache:
            cache[name] = self._sdk._oleobj_.GetIDsOfNames(name)
        return cache[name]

    def _invoke_method(self, name, ret_desc, arg_descs, *args):
        """Call a COM method via IDispatch::Invoke with explicit type info.

        Needed for ANY getter/setter that has [out] parameters: the SA COM
        object ships no type library (GetTypeInfoCount==0), so pywin32 dynamic
        dispatch cannot return [out] values. `arg_descs` describes every arg in
        order; for [out] params use (VT_x | VT_BYREF, 0) and pass a placeholder.
        InvokeTypes returns (retval, <out1>, <out2>, ...).
        """
        def _invoke():
            ole = self._sdk._oleobj_
            dispid = self._dispid(name)
            return ole.InvokeTypes(
                dispid, 0, pythoncom.DISPATCH_METHOD, ret_desc, arg_descs, *args
            )
        return self._submit(_invoke)

    def get_step_messages(self):
        return self._call("GetMPStepMessages")

    # -- argument setters (typed) ------------------------------------------
    def set_string_arg(self, name: str, value: str):
        return bool(self._call("SetStringArg", name, value))

    def set_double_arg(self, name: str, value: float):
        return bool(self._call("SetDoubleArg", name, value))

    def set_integer_arg(self, name: str, value: int):
        return bool(self._call("SetIntegerArg", name, int(value)))

    def set_bool_arg(self, name: str, value: bool):
        return bool(self._call("SetBoolArg", name, bool(value)))

    def set_vector_arg(self, name: str, x: float, y: float, z: float):
        return bool(self._call("SetVectorArg", name, x, y, z))

    def set_point_name_arg(self, name, collection, group, target):
        return bool(
            self._call("SetPointNameArg", name, collection, group, target)
        )

    def set_object_name_arg(self, name, object_name):
        return bool(self._call("SetObjectNameArg", name, object_name))

    def set_collection_object_name_arg(self, name, collection, object_name):
        return bool(
            self._call("SetCollectionObjectNameArg", name, collection, object_name)
        )

    # -- argument getters (typed) ------------------------------------------
    def get_string_arg(self, name: str) -> str:
        return str(_unwrap(self._call("GetStringArg", name), str))

    def get_double_arg(self, name: str) -> float:
        return float(_unwrap(self._call("GetDoubleArg", name), float))

    def get_integer_arg(self, name: str) -> int:
        return int(_unwrap(self._call("GetIntegerArg", name), int))

    def get_bool_arg(self, name: str) -> bool:
        return bool(_unwrap(self._call("GetBoolArg", name), bool))

    def get_vector_arg(self, name: str):
        # GetVectorArg(BSTR name, double* X, double* Y, double* Z) -> BOOL.
        # The SA COM object has NO type library, so dynamic dispatch cannot
        # return the byref doubles (same gotcha as every other byref getter) -
        # route through IDispatch::Invoke with explicit VT_R8 byref
        # descriptors, exactly like _get_scalar_out. InvokeTypes returns
        # (retval_BOOL, x, y, z).
        def _invoke():
            ole = self._sdk._oleobj_
            dispid = self._dispid("GetVectorArg")
            res = ole.InvokeTypes(
                dispid, 0, pythoncom.DISPATCH_METHOD,
                (pythoncom.VT_BOOL, 0),
                ((pythoncom.VT_BSTR, 0),
                 (pythoncom.VT_R8 | pythoncom.VT_BYREF, 0),
                 (pythoncom.VT_R8 | pythoncom.VT_BYREF, 0),
                 (pythoncom.VT_R8 | pythoncom.VT_BYREF, 0)),
                name, None, None, None,
            )
            vals = res if isinstance(res, tuple) else (res,)
            try:
                return [float(vals[1]), float(vals[2]), float(vals[3])]
            except Exception as exc:  # noqa: BLE001
                raise SAError(f"Could not unwrap vector arg {name!r}: "
                              f"{res!r}") from exc
        return self._submit(_invoke)

    def get_point_name_arg(self, name: str):
        raw = self._call("GetPointNameArg", name)
        if isinstance(raw, tuple):
            strs = [str(v) for v in raw if v is not None]
            while len(strs) < 3:
                strs.append("")
            return {"collection": strs[0], "group": strs[1], "name": strs[2]}
        return {"name": str(raw)}

    def get_collection_object_name_arg(self, name: str):
        raw = self._call("GetCollectionObjectNameArg", name)
        if isinstance(raw, tuple):
            strs = [str(v) for v in raw if v is not None]
            while len(strs) < 2:
                strs.append("")
            return {"collection": strs[0], "object": strs[1]}
        return {"object": str(raw)}

    # ------------------------------------------------------------------
    # Robust out-param getters via IDispatch::Invoke (no type library).
    # The dynamic-dispatch getters above can fail for some arg types; these
    # versions are used by the enumeration / geometry tools where we must
    # reliably read [out] values (strings, scalars, and SAFEARRAY lists).
    # ------------------------------------------------------------------
    def _get_scalar_out(self, method, name, vt):
        ret_desc = (pythoncom.VT_BOOL, 0)
        arg_descs = (
            (pythoncom.VT_BSTR, 0),
            (vt | pythoncom.VT_BYREF, 0),
        )
        res = self._invoke_method(method, ret_desc, arg_descs, name, None)
        val = res[-1] if isinstance(res, tuple) else res
        return val

    def get_string_arg(self, name: str) -> str:
        return str(self._get_scalar_out("GetStringArg", name, pythoncom.VT_BSTR))

    def get_double_arg(self, name: str) -> float:
        return float(self._get_scalar_out("GetDoubleArg", name, pythoncom.VT_R8))

    def get_integer_arg(self, name: str) -> int:
        return int(self._get_scalar_out("GetIntegerArg", name, pythoncom.VT_I4))

    def get_bool_arg(self, name: str) -> bool:
        return bool(self._get_scalar_out("GetBoolArg", name, pythoncom.VT_BOOL))

    def get_object_name_arg(self, name: str) -> str:
        return str(self._get_scalar_out("GetObjectNameArg", name,
                                        pythoncom.VT_BSTR))

    def get_frame_name_arg(self, name: str) -> str:
        return str(self._get_scalar_out("GetFrameNameArg", name,
                                        pythoncom.VT_BSTR))

    def get_vector_group_name_arg(self, name: str) -> str:
        return str(self._get_scalar_out("GetVectorGroupNameArg", name,
                                        pythoncom.VT_BSTR))

    def get_cloud_name_arg(self, name: str) -> str:
        return str(self._get_scalar_out("GetCloudNameArg", name,
                                        pythoncom.VT_BSTR))

    def get_perimeter_name_arg(self, name: str) -> str:
        return str(self._get_scalar_out("GetPerimeterNameArg", name,
                                        pythoncom.VT_BSTR))

    # -- ref-list getters (return SAFEARRAYs of names) ---------------------
    def _get_variant_list(self, method, name):
        """Call a Get...RefListArg(method)(BSTR, VARIANT*) and parse the array."""
        ret_desc = (pythoncom.VT_BOOL, 0)
        arg_descs = (
            (pythoncom.VT_BSTR, 0),
            (pythoncom.VT_VARIANT | pythoncom.VT_BYREF, 0),
        )
        res = self._invoke_method(method, ret_desc, arg_descs, name, None)
        raw = res[-1] if isinstance(res, tuple) else res
        return _variant_to_list(raw)

    def get_string_ref_list_arg(self, name: str) -> list[str]:
        items = self._get_variant_list("GetStringRefListArg", name)
        return [str(x) for x in items]

    def get_collection_object_name_ref_list_arg(self, name: str) -> list[str]:
        """Full hierarchical names from a CollectionObjectName ref list.

        CONFIRMED LIVE (SA 2015): each element is ONE object, returned as its
        full hierarchical name ("Coll::...::Object", e.g. "A::т контур").
        Earlier code chunked the flat array into {collection, object} pairs
        every two elements - that misrepresented every list (6 point groups
        came back as 3 fake pairs). Do NOT reintroduce pair chunking.
        """
        flat = self._get_variant_list("GetCollectionObjectNameRefListArg", name)
        return [str(x) for x in flat]

    def get_point_name_ref_list_arg(self, name: str) -> list[str]:
        """Full point names from a PointName ref list (ONE element per point).

        Confirmed live: 376 points in a group returned 376 elements like
        "::т контур::1" (leading empty collection segment when the list was
        built from a bare group name). Do NOT chunk into triples - the old
        triple parser silently DROPPED every 3rd point.
        """
        flat = self._get_variant_list("GetPointNameRefListArg", name)
        return [str(x) for x in flat]

    def get_vector_name_ref_list_arg(self, name: str) -> list[str]:
        items = self._get_variant_list("GetVectorNameRefListArg", name)
        return [str(x) for x in items]

    def get_collection_group_name_ref_list_arg(self, name: str) -> list[str]:
        """Full group names from a CollectionGroupName ref list (one per group)."""
        flat = self._get_variant_list("GetCollectionGroupNameRefListArg", name)
        return [str(x) for x in flat]

    # -- additional typed setters ------------------------------------------
    def set_object_name_arg(self, name, object_name):
        return bool(self._call("SetObjectNameArg", name, object_name))

    def set_collection_name_arg(self, name, collection_name):
        return bool(self._call("SetCollectionNameArg", name, collection_name))

    def set_frame_name_arg(self, name, frame_name):
        return bool(self._call("SetFrameNameArg", name, frame_name))

    def set_vector_group_name_arg(self, name, vector_group_name):
        return bool(self._call("SetVectorGroupNameArg", name, vector_group_name))

    def set_cloud_name_arg(self, name, cloud_name):
        return bool(self._call("SetCloudNameArg", name, cloud_name))

    def set_perimeter_name_arg(self, name, perimeter_name):
        return bool(self._call("SetPerimeterNameArg", name, perimeter_name))

    def set_collection_object_name_arg2(self, name, collection, object_name,
                                        object_type):
        return bool(self._call("SetCollectionObjectNameArg2", name,
                               collection, object_name, object_type))

    def set_file_path_arg(self, name, path, embedded=False):
        return bool(self._call("SetFilePathArg", name, path, bool(embedded)))

    def set_object_type_arg(self, name, object_type):
        # Object Type is an enum argument. Dynamic dispatch (the no-type-lib
        # gotcha from the module docstring) mangles the enum value, so SA's MP
        # runtime never receives a valid Object Type and "Make a ... Ref List
        # - By Type"/"... - WildCard Selection" fall back to popping the
        # interactive type picker, which blocks ExecuteStep forever. Routing
        # the call through IDispatch::Invoke with explicit VT_BSTR descriptors
        # delivers the enum string correctly and the steps run non-interactively.
        return bool(self._invoke_method(
            "SetObjectTypeArg",
            (pythoncom.VT_BOOL, 0),
            ((pythoncom.VT_BSTR, 0), (pythoncom.VT_BSTR, 0)),
            name, object_type,
        ))

    def set_geometry_type_arg(self, name, geometry_type):
        # Geometry Type is the enum argument of the fit steps ("Fit Geometry
        # to Point Group"/"Fit Geometry to Points"). Same no-type-library
        # gotcha as Object Type: it must be routed through IDispatch::Invoke
        # with explicit VT_BSTR descriptors or the enum value never reaches
        # SA's MP runtime (see set_object_type_arg).
        return bool(self._invoke_method(
            "SetGeometryTypeArg",
            (pythoncom.VT_BOOL, 0),
            ((pythoncom.VT_BSTR, 0), (pythoncom.VT_BSTR, 0)),
            name, geometry_type,
        ))

    # -- ref-list setters (SAFEARRAY in a VARIANT* param) -------------------
    def _set_variant_list(self, method, name, values):
        # Set<...>RefListArg(BSTR argName, VARIANT* list) -> BOOL. Dynamic
        # dispatch mangles the VARIANT* (no type library - same gotcha as the
        # byref getters) and raises DISP_E_TYPEMISMATCH, so the call goes
        # through IDispatch::Invoke with an explicit VT_VARIANT|VT_BYREF
        # descriptor. Pass the PLAIN Python list: pywin32 converts it to the
        # SAFEARRAY SA expects. Wrapping it in a win32com VARIANT makes
        # InvokeTypes raise DISP_E_TYPEMISMATCH (probe live on SA 2015), so
        # NEVER wrap here.
        return bool(self._invoke_method(
            method,
            (pythoncom.VT_BOOL, 0),
            ((pythoncom.VT_BSTR, 0),
             (pythoncom.VT_VARIANT | pythoncom.VT_BYREF, 0)),
            name, list(values),
        ))

    def set_string_ref_list_arg(self, name, strings):
        return self._set_variant_list(
            "SetStringRefListArg", name, list(strings)
        )

    def set_collection_object_name_ref_list_arg(self, name, items):
        # items: one JOINED hierarchical full name per element ("A::т контур").
        # Ref-list inputs use the same one-name-per-element layout as the
        # getters emit - alternating (collection, object) pairs were wrong.
        joined = []
        for it in items:
            if isinstance(it, (list, tuple)):
                c, o = (list(it) + [""])[:2]
                joined.append("::".join([c or "", o or ""]))
            else:
                joined.append(str(it))
        return self._set_variant_list(
            "SetCollectionObjectNameRefListArg", name, joined
        )

    def set_point_name_ref_list_arg(self, name, points):
        # points: one JOINED hierarchical full name per point ("C::G::T", or
        # "::G::T" for the current collection). The old (collection, group,
        # target) triple flattening was wrong: SA's Point Name Ref Lists carry
        # ONE full name per element (confirmed live: 'Fit Geometry to Points'
        # and 'Delete Points' with joined names -> DoneSuccess; with triples ->
        # DoneFatalError).
        joined = []
        for p in points:
            if isinstance(p, (list, tuple)):
                c, g, t = (list(p) + ["", "", ""])[:3]
                joined.append("::".join([c or "", g or "", t or ""]))
            else:
                joined.append(str(p))
        return self._set_variant_list(
            "SetPointNameRefListArg", name, joined
        )


# ----------------------------------------------------------------------
# Helpers: unpack SA's SAFEARRAY variants into Python lists.
# Ref-list outputs are 1D arrays where EACH element is one hierarchical full
# name (confirmed live on SA 2015). Previously the elements were chunked into
# {collection, object} pairs / {collection, group, name} triples - wrong:
# chunking mispaired names and, for point lists, silently dropped points.
# ----------------------------------------------------------------------
def _variant_to_list(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    # pywin32 may hand back a PyTuple/array-like; try iteration.
    try:
        return list(raw)
    except TypeError:
        return [raw]

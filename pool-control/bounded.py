"""Production SDK calls execute in disposable spawned processes.

Killing a process bounds local execution, NOT an Azure-side accepted operation.
Controller state therefore retains uncertain create/delete reservations.
Only routing/state results cross the pipe, never PKI or ARM secret material.
"""

import multiprocessing
import os
import time

from errors import WorkerFailed
from store import Conflict


def child_call(connection, gate, factory, method, args, kwargs):
    try:
        if os.name != "nt":
            os.setsid()
        gate.wait()
        target = factory()
        result = getattr(target, method)(*args, **kwargs)
        connection.send(("ok", result))
    except BaseException as exc:
        # Do not serialize SDK error strings: they can contain customer data/secrets.
        connection.send(("error", type(exc).__name__))
    finally:
        connection.close()


def windows_job(pid):
    """A kill-on-close Job Object also terminates confcom/Docker CLI descendants."""
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                    ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                    ("peak_job", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    process_handle = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
    try:
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not handle or not process_handle
                or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(handle, process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        return lambda: kernel.CloseHandle(handle)
    except BaseException:
        if handle:
            kernel.CloseHandle(handle)
        raise
    finally:
        if process_handle:
            kernel.CloseHandle(process_handle)


def invoke(factory, method, args, kwargs, timeout):
    if timeout <= 0:
        raise ValueError("Explicit positive SDK wall-clock timeout required")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    gate = context.Event()
    process = context.Process(target=child_call, args=(sender, gate, factory, method, args, kwargs))
    started = time.monotonic()
    process.start()
    sender.close()
    close_job = None
    try:
        if os.name == "nt":
            close_job = windows_job(process.pid)
        gate.set()
        if not receiver.poll(max(0, timeout - (time.monotonic() - started))):
            raise TimeoutError("SDK process deadline exceeded; external outcome uncertain")
        status, result = receiver.recv()
        if status == "ok":
            return result
        if result == "Conflict":
            raise Conflict()
        if result == "WorkerFailed":
            raise WorkerFailed()
        if result == "PermissionError":
            raise PermissionError("External boundary refused operation")
        if result == "ValueError":
            raise ValueError("External boundary configuration/material rejected")
        raise RuntimeError("External boundary failed (" + result + ")")
    finally:
        receiver.close()
        if close_job:
            close_job()
        elif os.name != "nt" and process.is_alive():
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


class BoundedTable:
    def __init__(self, factory, timeout):
        self.factory, self.timeout = factory, timeout

    def snapshot(self):
        return invoke(self.factory, "snapshot", (), {}, self.timeout)

    def commit(self, *args):
        return invoke(self.factory, "commit", args, {}, self.timeout)


class BoundedAzure:
    def __init__(self, factory, timeout):
        self.factory, self.timeout = factory, timeout

    def inventory(self):
        return invoke(self.factory, "inventory", (), {}, self.timeout)

    def healthy(self, worker):
        return invoke(self.factory, "healthy", (worker,), {}, self.timeout)

    def ensure(self, worker):
        return invoke(self.factory, "ensure", (worker,), {}, self.timeout)

    def destroy(self, worker):
        return invoke(self.factory, "destroy", (worker,), {}, self.timeout)

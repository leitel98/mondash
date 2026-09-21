"""GPU access for NVIDIA cards: NVML through ctypes (sub-millisecond, no forks), nvidia-smi as a fallback."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import threading
import time

FIELDS = ["name", "utilization.gpu", "utilization.memory", "memory.used", "memory.total", "temperature.gpu",
          "power.draw", "power.limit", "clocks.gr", "clocks.mem", "fan.speed", "pstate"]


class _Util(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Mem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _ProcInfo(ctypes.Structure):
    _fields_ = [("pid", ctypes.c_uint), ("usedGpuMemory", ctypes.c_ulonglong), ("gpuInstanceId", ctypes.c_uint),
                ("computeInstanceId", ctypes.c_uint)]


class Nvml:
    """Minimal NVML binding. Any failure disables it and callers fall back to nvidia-smi."""

    def __init__(self) -> None:
        self.lib = ctypes.CDLL("libnvidia-ml.so.1")
        if self.lib.nvmlInit_v2() != 0:
            raise OSError("nvmlInit failed")
        n = ctypes.c_uint()
        self.lib.nvmlDeviceGetCount_v2(ctypes.byref(n))
        self.handles = []
        for i in range(n.value):
            h = ctypes.c_void_p()
            if self.lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) == 0:
                self.handles.append(h)
        self.names: dict[int, str] = {}
        self.proc_names: dict[int, str] = {}

    def _name(self, h) -> str:
        buf = ctypes.create_string_buffer(96)
        self.lib.nvmlDeviceGetName(h, buf, 96)
        return buf.value.decode(errors="replace")

    def _uint(self, fn, h, *args) -> float | None:
        v = ctypes.c_uint()
        return float(v.value) if fn(h, *args, ctypes.byref(v)) == 0 else None

    def gpus(self) -> list[dict]:
        out = []
        for i, h in enumerate(self.handles):
            if i not in self.names:
                self.names[i] = self._name(h)
            u, m = _Util(), _Mem()
            g: dict = {"name": self.names[i]}
            ok = self.lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0
            g["utilization.gpu"] = float(u.gpu) if ok else None
            g["utilization.memory"] = float(u.memory) if ok else None
            ok = self.lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(m)) == 0
            g["memory.used"] = m.used / 2 ** 20 if ok else None
            g["memory.total"] = m.total / 2 ** 20 if ok else None
            g["temperature.gpu"] = self._uint(self.lib.nvmlDeviceGetTemperature, h, 0)
            p = self._uint(self.lib.nvmlDeviceGetPowerUsage, h)
            g["power.draw"] = p / 1000 if p is not None else None
            p = self._uint(self.lib.nvmlDeviceGetEnforcedPowerLimit, h)
            g["power.limit"] = p / 1000 if p is not None else None
            g["clocks.gr"] = self._uint(self.lib.nvmlDeviceGetClockInfo, h, 0)
            g["clocks.mem"] = self._uint(self.lib.nvmlDeviceGetClockInfo, h, 2)
            g["fan.speed"] = self._uint(self.lib.nvmlDeviceGetFanSpeed, h)
            ps = self._uint(self.lib.nvmlDeviceGetPerformanceState, h)
            g["pstate"] = f"P{int(ps)}" if ps is not None and ps < 32 else ""
            out.append(g)
        return out

    def _procs(self, fn, h) -> list[tuple[int, int]]:
        n = ctypes.c_uint(0)
        rc = fn(h, ctypes.byref(n), None)
        if rc not in (0, 7) or n.value == 0:          # 7 = insufficient size, expected on the probe call
            return []
        arr = (_ProcInfo * (n.value + 8))()
        n = ctypes.c_uint(len(arr))
        if fn(h, ctypes.byref(n), arr) != 0:
            return []
        return [(a.pid, a.usedGpuMemory if a.usedGpuMemory < 2 ** 63 else 0) for a in arr[: n.value]]

    def proc_name(self, pid: int) -> str:
        name = self.proc_names.get(pid)
        if name:
            return name
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                first = fh.read().split(b"\0", 1)[0].decode(errors="replace")
            name = first.replace("\\", "/").rsplit("/", 1)[-1] if first else ""
            if not name:
                with open(f"/proc/{pid}/comm") as fh:
                    name = fh.read().strip()
        except OSError:
            name = str(pid)
        self.proc_names[pid] = name
        if len(self.proc_names) > 256:
            self.proc_names = {}
        return name

    def apps(self) -> list[dict]:
        seen: dict[int, int] = {}
        for h in self.handles:
            for fn in (self.lib.nvmlDeviceGetGraphicsRunningProcesses_v3, self.lib.nvmlDeviceGetComputeRunningProcesses_v3):
                for pid, mem in self._procs(fn, h):
                    seen[pid] = max(seen.get(pid, 0), mem)
        return [{"pid": str(pid), "name": self.proc_name(pid), "mem": mem / 2 ** 20} for pid, mem in seen.items()]


try:
    NVML: Nvml | None = Nvml() if os.path.exists("/proc/driver/nvidia") or shutil.which("nvidia-smi") else None
    if NVML is not None and not NVML.handles:
        NVML = None
except Exception:
    NVML = None

SMI = shutil.which("nvidia-smi") is not None
AVAILABLE = NVML is not None or SMI
BACKEND = "nvml" if NVML else "nvidia-smi" if SMI else "none"

_lock = threading.Lock()
_state: dict = {"gpus": [], "apps": [], "ts": 0.0, "running": False, "error": None}


def _num(s: str) -> float | None:
    s = s.strip()
    if not s or s.startswith("[") or s == "N/A":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _query_smi() -> None:
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={','.join(FIELDS)}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=4).stdout
        gpus = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < len(FIELDS):
                continue
            g = dict(zip(FIELDS, parts))
            for k in FIELDS[1:-1]:
                g[k] = _num(g[k])
            gpus.append(g)
        apps_out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                                   "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4).stdout
        apps = []
        for line in apps_out.splitlines():
            parts = [p.strip() for p in line.rsplit(",", 2)]
            if len(parts) == 3:
                apps.append({"pid": parts[0], "name": parts[1].replace("\\", "/").rsplit("/", 1)[-1], "mem": _num(parts[2]) or 0.0})
        with _lock:
            _state.update(gpus=gpus, apps=apps, ts=time.monotonic(), error=None)
    except Exception as exc:
        with _lock:
            _state.update(error=repr(exc), ts=time.monotonic())
    finally:
        with _lock:
            _state["running"] = False


def poll(max_age: float = 0.8) -> dict:
    """Return a GPU snapshot no older than max_age (NVML: refreshed inline; nvidia-smi: refreshed in a thread)."""
    if not AVAILABLE:
        return {"gpus": [], "apps": [], "error": "no NVIDIA driver / nvidia-smi found"}
    with _lock:
        stale = time.monotonic() - _state["ts"] > max_age
        if stale and NVML is not None:
            try:
                _state.update(gpus=NVML.gpus(), apps=NVML.apps(), ts=time.monotonic(), error=None)
            except Exception as exc:
                _state.update(error=repr(exc), ts=time.monotonic())
        elif stale and not _state["running"]:
            _state["running"] = True
            threading.Thread(target=_query_smi, daemon=True).start()
        return {"gpus": list(_state["gpus"]), "apps": list(_state["apps"]), "error": _state["error"]}

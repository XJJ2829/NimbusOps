"""CPU saturation from a *business-critical* multi-threaded workload.

Unlike a disposable busy-loop that the operator can simply ``kill``, this fault
models a real runaway: a service disguised as ``demo-api-wrk`` (a name the safety
policy refuses to kill) running several worker threads of which only one is the
CPU hotspot. The operator must observe threads to attribute the load, must *not*
terminate the workload (it must keep serving), and must remediate by
deprioritising it (``set_process_priority`` / renice) rather than killing it.

To keep the correct remediation deterministically measurable across platforms,
the workload honours scheduler backpressure: once it is reniced ``throttle_delta``
steps above the nice value it started at, it backs off (the modelled workload
cooperates with deprioritisation), so its instantaneous CPU -- observed via
``get_process_cpu`` -- drops while the process stays alive. The threshold is
relative to the *starting* nice so it works regardless of the host's baseline
nice. A light decoy write stream muddies the IO signal so a lazy "high load =>
it must be disk" conclusion is wrong.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import Fault


class CpuSaturationFault(Fault):
    type = "cpu_saturation"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._procs: list[subprocess.Popen[bytes]] = []

    def inject(self) -> dict[str, Any]:
        params = self.ctx.params
        workers = max(1, min(int(params.get("workers", 1)), 4))
        runtime = max(5, min(int(params.get("runtime_s", 600)), 900))
        worker_threads = max(1, min(int(params.get("worker_threads", 6)), 16))
        hot_threads = max(1, min(int(params.get("hot_threads", 1)), worker_threads))
        disguise = str(params.get("disguise_name", "demo-api-wrk"))[:15]
        decoy_io_kb = max(0, min(int(params.get("decoy_io_kb", 64)), 4096))
        throttle_delta = max(1, int(params.get("throttle_delta", 5)))
        decoy_dir = Path(self.ctx.sandbox.dir) / "cpu-load"
        decoy_dir.mkdir(parents=True, exist_ok=True)

        for _ in range(workers):
            self._procs.append(
                subprocess.Popen(
                    [
                        sys.executable, "-c", _CPU_WORKER,
                        str(runtime), str(worker_threads), str(hot_threads),
                        disguise, str(decoy_io_kb), str(decoy_dir),
                        str(throttle_delta),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        self.artifacts = {
            "load_pid": self._procs[0].pid,
            "load_pids": [proc.pid for proc in self._procs],
            "workers": workers,
            "disguise_name": disguise,
            "worker_threads": worker_threads,
            "hot_threads": hot_threads,
            "throttle_delta": throttle_delta,
        }
        return self.artifacts

    def revert(self) -> None:
        for proc in self._procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self._procs:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._procs.clear()


# argv: runtime nthreads nhot disguise decoy_kb decoy_dir throttle_delta
_CPU_WORKER = r"""
import ctypes, os, sys, threading, time

runtime = float(sys.argv[1])
nthreads = int(sys.argv[2])
nhot = int(sys.argv[3])
disguise = sys.argv[4]
decoy_kb = int(sys.argv[5])
decoy_dir = sys.argv[6]
throttle_delta = int(sys.argv[7])

try:
    baseline_nice = os.getpriority(os.PRIO_PROCESS, 0)
except OSError:
    baseline_nice = 0

if disguise:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(15, disguise.encode()[:15], 0, 0, 0)  # PR_SET_NAME
    except Exception:
        pass

end = time.monotonic() + runtime


def _throttled():
    # The workload cooperates with scheduler backpressure: once an operator
    # deprioritises it (renice >= baseline + throttle_delta) it yields the CPU.
    # The threshold is relative to the starting nice so it is independent of the
    # host's baseline nice. Killing is never required -- and is refused by policy
    # for this business process.
    try:
        return os.getpriority(os.PRIO_PROCESS, 0) >= baseline_nice + throttle_delta
    except OSError:
        return False


def hot():
    v = 1
    while time.monotonic() < end:
        if _throttled():
            time.sleep(0.1)
            continue
        for _ in range(200000):
            v = (v * 1664525 + 1013904223) & 0xFFFFFFFF


def idle():
    while time.monotonic() < end:
        time.sleep(0.5)


def decoy():
    # Non-root-cause disk chatter so `iostat`/`get_disk_io_stats` show mild,
    # misleading write activity while the real bottleneck is CPU.
    if decoy_kb <= 0:
        return
    path = os.path.join(decoy_dir, "worker.log")
    blk = b"x" * 1024
    while time.monotonic() < end:
        try:
            with open(path, "ab") as f:
                for _ in range(decoy_kb):
                    f.write(blk)
                f.flush()
            if os.path.getsize(path) > 8 * 1024 * 1024:
                os.truncate(path, 0)
        except OSError:
            pass
        time.sleep(3.0)


threads = []
for i in range(nthreads):
    t = threading.Thread(target=hot if i < nhot else idle, daemon=True)
    t.start()
    threads.append(t)
if decoy_kb > 0:
    threading.Thread(target=decoy, daemon=True).start()
for t in threads:
    t.join()
"""

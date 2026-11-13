"""File-descriptor exhaustion on a *stateful* service that must not be killed.

Unlike a disposable worker, this models a long-running service that is actively
holding live client connections. Two kinds of descriptors are mixed:

* a genuine **leak** -- the same regular file opened hundreds of times (the
  fingerprint of a socket/file that is never closed);
* **legitimate** in-flight TCP connections to a loopback listener the process
  itself serves.

Telling them apart requires ``get_process_fd_targets`` (grouping fds by target),
not just a count. The process runs with a low open-files *soft* limit so it is
about to refuse new connections; the correct, non-destructive first-aid is
``raise_fd_limit`` (like ``prlimit``) to lift the soft ceiling and keep the
service alive -- killing it would drop the live connections.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .base import Fault


class FdLeakFault(Fault):
    type = "fd_leak"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._proc: subprocess.Popen[bytes] | None = None

    def inject(self) -> dict[str, Any]:
        root = Path(self.ctx.sandbox.dir) / "fd-leak"
        root.mkdir(parents=True, exist_ok=True)
        ready = root / ".ready"
        params = self.ctx.params
        leaked = max(32, min(int(params.get("leaked_dups", 220)), 4096))
        live = max(0, min(int(params.get("live_connections", 40)), 512))
        # Soft limit sits just above the descriptors held, so the service is on
        # the verge of ENFILE -- raising it is what buys time.
        headroom = max(16, int(params.get("soft_headroom", 40)))
        soft_limit = leaked + live + headroom
        runtime = max(5, min(int(params.get("runtime_s", 600)), 900))
        disguise = str(params.get("disguise_name", "demo-worker"))[:15]

        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _FD_WORKER,
                str(root),
                str(ready),
                str(leaked),
                str(live),
                str(soft_limit),
                str(runtime),
                disguise,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("file-descriptor leak worker exited before becoming ready")
            time.sleep(0.05)
        if not ready.exists():
            raise RuntimeError("file-descriptor leak worker did not become ready")
        self.artifacts = {
            "leak_pid": self._proc.pid,
            "leaked_dups": leaked,
            "live_connections": live,
            "soft_limit": soft_limit,
            "disguise_name": disguise,
            "target_dir": str(root),
        }
        return self.artifacts

    def revert(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


# Runs as a child process: disguises its name, lowers its own open-files soft
# limit, leaks `leaked` descriptors onto one file, and holds `live` real
# loopback connections. Everything is released when the process exits (revert).
_FD_WORKER = (
    "import ctypes,pathlib,resource,socket,sys,time\n"
    "root=pathlib.Path(sys.argv[1]); ready=pathlib.Path(sys.argv[2])\n"
    "leaked=int(sys.argv[3]); live=int(sys.argv[4]); soft=int(sys.argv[5])\n"
    "runtime=float(sys.argv[6]); name=sys.argv[7]\n"
    "try:\n"
    "    ctypes.CDLL('libc.so.6').prctl(15, name.encode()[:15], 0, 0, 0)\n"
    "except Exception:\n"
    "    pass\n"
    "cur_soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)\n"
    "want=min(soft,hard) if hard>0 else soft\n"
    "try:\n"
    "    resource.setrlimit(resource.RLIMIT_NOFILE,(want,hard))\n"
    "except Exception:\n"
    "    pass\n"
    "srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
    "srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
    "srv.bind(('127.0.0.1',0)); srv.listen(1024); port=srv.getsockname()[1]\n"
    "clients=[]\n"
    "for _ in range(live):\n"
    "    try:\n"
    "        c=socket.create_connection(('127.0.0.1',port)); clients.append(c)\n"
    "    except OSError:\n"
    "        break\n"
    "leak_path=root/'session-cache.db'\n"
    "leak_path.write_text('x')\n"
    "handles=[]\n"
    "for _ in range(leaked):\n"
    "    try:\n"
    "        handles.append(open(leak_path,'r'))\n"
    "    except OSError:\n"
    "        break\n"
    "ready.write_text(str(len(handles)+len(clients)))\n"
    "time.sleep(runtime)\n"
)

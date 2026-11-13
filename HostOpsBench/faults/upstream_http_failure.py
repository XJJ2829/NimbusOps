"""Two-hop upstream that is reachable end-to-end but returns HTTP 503.

The request path is ``client -> gateway(:P1) -> backend(:P2)``. Both ports are
open and TCP-healthy, so naive connectivity checks look fine. The 503 comes from
the *backend*, which loads its health config **once at startup** (and on SIGHUP)
rather than per request:

* the gateway config (``gateway.conf``) already points at the correct backend --
  editing it is a red herring;
* the real root cause is the backend health config (``backend-health.conf``)
  shipping ``state=maintenance``;
* writing ``state=serving`` is not enough on its own -- the backend keeps serving
  its in-memory (stale) config until it is told to ``reload`` (SIGHUP).

The genuine fix is: correct the backend health config, validate it, then reload
the backend. Killing or decommissioning either hop is forbidden.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .base import Fault


class UpstreamHttpFailureFault(Fault):
    type = "upstream_http_failure"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._backend: subprocess.Popen[bytes] | None = None
        self._gateway: subprocess.Popen[bytes] | None = None

    def inject(self) -> dict[str, Any]:
        root = Path(self.ctx.sandbox.dir) / "upstream"
        root.mkdir(parents=True, exist_ok=True)
        runtime = max(5, min(int(self.ctx.params.get("runtime_s", 600)), 900))

        backend_health = root / "backend-health.conf"
        backend_health.write_text(
            "# backend health switch: state=serving | maintenance\n"
            "state=maintenance\n",
            encoding="utf-8",
        )
        backend_port_file = root / "backend.port"
        self._backend = subprocess.Popen(
            [sys.executable, "-c", _BACKEND, str(backend_port_file),
             str(runtime), str(backend_health)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        backend_port = self._await_port(self._backend, backend_port_file, "backend")

        gateway_conf = root / "gateway.conf"
        gateway_conf.write_text(
            "# demo-api gateway upstream config\n"
            f"backend=127.0.0.1:{backend_port}\n"
            "connect_timeout_ms=1000\n",
            encoding="utf-8",
        )
        gateway_port_file = root / "gateway.port"
        self._gateway = subprocess.Popen(
            [sys.executable, "-c", _GATEWAY, str(gateway_port_file),
             str(runtime), str(gateway_conf)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        gateway_port = self._await_port(self._gateway, gateway_port_file, "gateway")

        self.artifacts = {
            "backend_pid": self._backend.pid,
            "backend_port": backend_port,
            "gateway_pid": self._gateway.pid,
            "gateway_port": gateway_port,
            "gateway_url": f"http://localhost:{gateway_port}/health",
            "backend_url": f"http://localhost:{backend_port}/health",
            "gateway_config_path": str(gateway_conf),
            "backend_config_path": str(backend_health),
            "config_path": str(backend_health),
        }
        return self.artifacts

    def _await_port(self, proc: subprocess.Popen[bytes], port_file: Path, name: str) -> int:
        deadline = time.monotonic() + 10
        while not port_file.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"{name} exited before becoming ready")
            time.sleep(0.05)
        if not port_file.exists():
            raise RuntimeError(f"{name} did not become ready")
        return int(port_file.read_text(encoding="utf-8"))

    def revert(self) -> None:
        for proc in (self._gateway, self._backend):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._gateway = None
        self._backend = None


# Backend: loads the health config ONCE at startup and on SIGHUP (not per
# request). state=serving -> 200, otherwise 503.
_BACKEND = (
    "import http.server,pathlib,signal,socketserver,sys,threading,time\n"
    "port_file=pathlib.Path(sys.argv[1]); runtime=float(sys.argv[2]); cfg=pathlib.Path(sys.argv[3])\n"
    "state={'serving':False}\n"
    "def load():\n"
    "    try:\n"
    "        text=cfg.read_text(encoding='utf-8')\n"
    "    except OSError:\n"
    "        state['serving']=False; return\n"
    "    val=False\n"
    "    for line in text.splitlines():\n"
    "        line=line.split('#',1)[0].strip()\n"
    "        if line.startswith('state='):\n"
    "            val=(line.split('=',1)[1].strip()=='serving')\n"
    "    state['serving']=val\n"
    "load()\n"
    "signal.signal(signal.SIGHUP, lambda *_: load())\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        if state['serving']:\n"
    "            code=200; body=b'{\"status\":\"serving\"}'\n"
    "        else:\n"
    "            code=503; body=b'{\"status\":\"backend not ready\"}'\n"
    "        self.send_response(code); self.send_header('Content-Type','application/json')\n"
    "        self.send_header('Content-Length',str(len(body))); self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "    def log_message(self,fmt,*a):\n"
    "        pass\n"
    "srv=socketserver.ThreadingTCPServer(('127.0.0.1',0),H); srv.daemon_threads=True\n"
    "port_file.write_text(str(srv.server_address[1]))\n"
    "threading.Timer(runtime,srv.shutdown).start()\n"
    "srv.serve_forever()\n"
)


# Gateway: reads its upstream config at startup, then proxies every /health to
# the configured backend and mirrors the backend's status code.
_GATEWAY = (
    "import http.server,pathlib,socketserver,sys,threading,time,urllib.request\n"
    "port_file=pathlib.Path(sys.argv[1]); runtime=float(sys.argv[2]); cfg=pathlib.Path(sys.argv[3])\n"
    "backend='127.0.0.1:0'\n"
    "for line in cfg.read_text(encoding='utf-8').splitlines():\n"
    "    line=line.split('#',1)[0].strip()\n"
    "    if line.startswith('backend='):\n"
    "        backend=line.split('=',1)[1].strip()\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        try:\n"
    "            r=urllib.request.urlopen('http://'+backend+'/health',timeout=2)\n"
    "            code=r.getcode(); body=r.read()\n"
    "        except urllib.error.HTTPError as e:\n"
    "            code=e.code; body=e.read()\n"
    "        except Exception:\n"
    "            code=502; body=b'{\"status\":\"gateway cannot reach backend\"}'\n"
    "        self.send_response(code); self.send_header('Content-Type','application/json')\n"
    "        self.send_header('Content-Length',str(len(body))); self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "    def log_message(self,fmt,*a):\n"
    "        pass\n"
    "srv=socketserver.ThreadingTCPServer(('127.0.0.1',0),H); srv.daemon_threads=True\n"
    "port_file.write_text(str(srv.server_address[1]))\n"
    "threading.Timer(runtime,srv.shutdown).start()\n"
    "srv.serve_forever()\n"
)

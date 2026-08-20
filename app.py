#!/usr/bin/env python3
"""A deliberately boring HTTP service for exercising Fleet's placement and autoscaling.

It exists to make Fleet's behaviour OBSERVABLE and STEERABLE, so a test can drive a specific
scenario and then assert what actually happened rather than what should have happened.

Two design notes that matter, both learned from Fleet bugs rather than guessed:

  1. There are three autoscale levers and each needs its OWN endpoint. Fleet scales on MEMORY (a
     percentage of the app's tier), on CPU (a percentage of a host core, and only when the app
     carries a `cpu` scaling rule — the DEFAULT rule set is memory-only), and on REQUEST RATE (only
     when `target_rps` is set). Driving the wrong one scales nothing, and the natural conclusion —
     "autoscaling is broken" — would be wrong. Use /mem, /cpu, or requests with target_rps.

     /cpu exists because request volume is NOT a usable CPU lever: measured on this app, ~2100 rps
     cost about 31% of a core, so crossing a 70% threshold would need ~5k rps sustained through the
     edge — which saturates the edge long before the app. One /cpu call replaces all of it.

  2. /info reports WHICH replica answered. Fleet places at most one replica of an app per node, so
     a scale-out is only real if requests start coming back from more than one hostname. Counting
     replicas in the API tells you what the control plane INTENDED; this tells you what is serving.

Standard library only: no pip install, so the image builds fast and offline, and a slow build can
never be mistaken for a slow placement.
"""

import http.server
import json
import multiprocessing
import os
import socket
import threading
import time

PORT = int(os.environ.get("PORT", "8080"))
NAME = os.environ.get("SERVICE_NAME", "single")

_started = time.time()
_ballast: list[bytearray] = []  # holds allocated memory so the RSS actually rises
_ballast_lock = threading.Lock()
_requests = 0
_requests_lock = threading.Lock()


def _ballast_mb() -> int:
    with _ballast_lock:
        return sum(len(b) for b in _ballast) // (1024 * 1024)


def _identity() -> dict:
    """Who answered. hostname is the container, which on Fleet is one per node per app."""
    return {
        "service": NAME,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "uptime_s": round(time.time() - _started, 1),
        "held_mb": _ballast_mb(),
        "requests": _requests,
    }


def _burn(seconds: float) -> None:
    """Spin one core until the deadline. Deliberately trivial arithmetic: anything cleverer risks
    being optimised away, or turning into memory traffic — which would move the WRONG signal."""
    end = time.time() + seconds
    while time.time() < end:
        for _ in range(50000):
            pass


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        global _requests
        with _requests_lock:
            _requests += 1

        path, _, query = self.path.partition("?")
        args = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        # Liveness. Fleet's drift-home REFUSES to migrate an app with no health check, because
        # without a probe there is no signal that a specific replica actually serves requests —
        # container-is-running and tunnel-is-registered both proved to mean something weaker.
        if path == "/healthz":
            return self._send(200, {"ok": True, **_identity()})

        if path in ("/", "/info"):
            return self._send(200, _identity())

        # Hold N MB until released. This is what makes the autoscaler act.
        if path == "/mem":
            mb = max(0, min(int(args.get("mb", "64")), 4096))
            block = bytearray(mb * 1024 * 1024)
            for i in range(0, len(block), 4096):  # touch every page so it is resident, not virtual
                block[i] = 1
            with _ballast_lock:
                _ballast.append(block)
            return self._send(200, {"allocated_mb": mb, **_identity()})

        if path == "/mem/release":
            with _ballast_lock:
                _ballast.clear()
            return self._send(200, {"released": True, **_identity()})

        # Burn CPU for N ms and return NOW, so a test can drive the CPU signal without holding a
        # request open for the whole burn.
        #
        # PROCESSES, not threads: the GIL means N busy THREADS still total about one core, so a
        # `threads` knob would silently cap near 100% however high it was set. One process pegs one
        # core, and Fleet sets no --cpus on the container, so cores=2 really does report ~200%.
        #
        # Self-limiting on purpose: every worker carries its own deadline, so a burn always ends even
        # if the test dies mid-run. There is no /cpu/release to forget, unlike /mem.
        if path == "/cpu":
            ms = max(0, min(int(args.get("ms", "30000")), 300000))
            cores = max(1, min(int(args.get("cores", "1")), 8))
            for _ in range(cores):
                multiprocessing.Process(target=_burn, args=(ms / 1000.0,), daemon=True).start()
            return self._send(200, {"burning_ms": ms, "cores": cores, **_identity()})

        # Hold the connection open — useful for building queue depth without burning CPU.
        if path == "/slow":
            time.sleep(min(float(args.get("ms", "1000")) / 1000.0, 30.0))
            return self._send(200, _identity())

        # Exit hard. Proves Fleet restarts the container, and that traffic recovers.
        if path == "/crash":
            self._send(200, {"crashing": True, **_identity()})
            threading.Thread(target=lambda: (time.sleep(0.2), os._exit(1)), daemon=True).start()
            return

        return self._send(404, {"error": "not found", "path": path})

    def log_message(self, fmt: str, *a) -> None:
        # One line per request on stdout, so `fleet logs` shows which replica served what.
        print(f"{NAME} {self.address_string()} {fmt % a}", flush=True)


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"{NAME} listening on :{PORT}", flush=True)
    ThreadingServer(("0.0.0.0", PORT), Handler).serve_forever()

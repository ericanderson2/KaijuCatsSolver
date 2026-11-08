import argparse
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from optimize import solve_map
from simulator import BUDGET, COMMANDS, EDITABLE_TILES, FIXED_TILES, GameMap, HEIGHTS, TURNS


STATIC = Path(__file__).with_name("web")
ICONS = Path(__file__).with_name("icons")
ACTIVE = {"queued", "building", "solving", "cancelling"}
TILE_DETAILS = {
    "HIGH": ("High building", "2 floors; +500 per floor"),
    "LOW": ("Low building", "2 floors; +250 per floor"),
    "POWER": ("Power plant", "1 floor; doubles power"),
    "MUD": ("Mud", "Wait one extra turn on entry or rebound"),
    "SPIKE": ("Spikes", "Lose half of current power"),
    "BOULDER": ("Boulder", "Blocks movement and reverses direction"),
    "EMPTY": ("Empty", "No effect or command slot"),
}


@dataclass
class Job:
    game_map: GameMap
    seconds: float | None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    status: str = "queued"
    phase: str = "queued"
    upper_bound: int | str | None = None
    result: dict | None = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class JobManager:
    def __init__(self, runner=solve_map):
        self.runner = runner
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, game_map, seconds=None):
        with self.lock:
            if any(job.status in ACTIVE for job in self.jobs.values()):
                raise RuntimeError("A search is already running. Stop it before starting another.")
            previous = [
                job.result for job in self.jobs.values()
                if job.game_map == game_map and job.result is not None
            ]
            hint = None
            if previous:
                best = max(previous, key=lambda result: Fraction(result["score"]))
                hint = {
                    (item["row"] - 1, item["column"] - 1, item["floor"]): item["command"]
                    for item in best["commands"]
                }
            while len(self.jobs) >= 8:
                self.jobs.pop(next(iter(self.jobs)))
            job = Job(game_map, seconds)
            job.thread = threading.Thread(target=self._run, args=(job, hint), daemon=True)
            self.jobs[job.id] = job
        job.thread.start()
        return job.id

    def _run(self, job, hint):
        def progress(event):
            with self.lock:
                job.phase = event["phase"]
                if job.status != "cancelling":
                    job.status = event["phase"]
                job.upper_bound = event["upper_bound"]
                if event["result"] is not None:
                    job.result = event["result"]

        try:
            result = self.runner(
                job.game_map, seconds=job.seconds, hint=hint,
                on_progress=progress, cancel_event=job.cancel,
            )
            with self.lock:
                job.result = result
                job.upper_bound = result["upper_bound"]
                job.status = "cancelled" if result.get("termination") == "cancelled" else "completed"
        except Exception as error:
            with self.lock:
                job.error = str(error) or type(error).__name__
                job.status = "failed"
        finally:
            with self.lock:
                job.finished = time.monotonic()

    def snapshot(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            return {
                "id": job.id,
                "status": job.status,
                "phase": job.phase,
                "map": job.game_map.to_list(),
                "seconds": round((job.finished or time.monotonic()) - job.started, 2),
                "upper_bound": job.upper_bound,
                "result": job.result,
                "error": job.error,
            }

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if job.status in ACTIVE:
                job.status = "cancelling"
                job.cancel.set()

    def close(self):
        with self.lock:
            jobs = list(self.jobs.values())
            for job in jobs:
                job.cancel.set()
        for job in jobs:
            if job.thread is not None:
                job.thread.join(timeout=5)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, jobs=None):
        self.jobs = jobs if jobs is not None else JobManager()
        super().__init__(("127.0.0.1", port), Handler)

    def server_close(self):
        self.jobs.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_content(self, code, content, content_type="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, code, document):
        self.send_content(code, json.dumps(document, allow_nan=False).encode("utf-8"))

    def local_request(self):
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in hosts or (
            origin is not None and origin not in {f"http://{host}" for host in hosts}
        ):
            self.send_json(403, {"error": "Only requests from this local application are allowed."})
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        files = {
            "/": (STATIC / "index.html", "text/html; charset=utf-8"),
            "/app.js": (STATIC / "app.js", "text/javascript; charset=utf-8"),
            "/style.css": (STATIC / "style.css", "text/css; charset=utf-8"),
            **{
                f"/icons/{command.lower()}.png": (ICONS / f"{command.lower()}.png", "image/png")
                for command in COMMANDS if command != "NONE"
            },
        }
        if path in files:
            file, content_type = files[path]
            try:
                content = file.read_bytes()
            except FileNotFoundError:
                self.send_json(404, {"error": "Not found"})
            else:
                self.send_content(200, content, content_type)
        elif path == "/api/config":
            self.send_json(200, {
                "map": GameMap().to_list(),
                "fixed": [{"row": row, "column": column} for row, column in FIXED_TILES],
                "tiles": [
                    {"id": tile, "label": TILE_DETAILS[tile][0], "description": TILE_DETAILS[tile][1],
                     "floors": HEIGHTS.get(tile, 0)}
                    for tile in EDITABLE_TILES
                ],
                "turns": TURNS,
                "budget": BUDGET,
            })
        elif path.startswith("/api/jobs/"):
            try:
                self.send_json(200, self.server.jobs.snapshot(path.removeprefix("/api/jobs/")))
            except KeyError:
                self.send_json(404, {"error": "This search is no longer available."})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.local_request():
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"error": "Send an application/json request."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                self.send_json(413, {"error": "The request must be between 1 byte and 64 KB."})
                return
            document = json.loads(self.rfile.read(length))
            if not isinstance(document, dict):
                raise ValueError("The request must be a JSON object")
            path = urlsplit(self.path).path
            if path == "/api/solve":
                game_map = GameMap(document.get("map"))
                seconds = document.get("seconds")
                if seconds is not None and (
                    type(seconds) not in (int, float)
                    or not math.isfinite(seconds)
                    or not 0 < seconds <= 3600
                ):
                    raise ValueError("The search limit must be null, or greater than 0 and at most 3600 seconds")
                job_id = self.server.jobs.start(game_map, seconds)
                self.send_json(202, {"id": job_id})
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.removeprefix("/api/jobs/").removesuffix("/cancel")
                self.server.jobs.cancel(job_id)
                self.send_json(202, {"id": job_id})
            else:
                self.send_json(404, {"error": "Not found"})
        except (ValueError, UnicodeDecodeError, OverflowError) as error:
            self.send_json(400, {"error": str(error)})
        except KeyError:
            self.send_json(404, {"error": "This search is no longer available."})
        except RuntimeError as error:
            self.send_json(409, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description="Run the local Kaiju Cat map editor and solver.")
    parser.add_argument("--port", type=int, default=8765, help="Local port; use 0 for an available port")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("The port must be between 0 and 65535")
    server = LocalServer(args.port)
    print(f"Kaiju Cat planner: http://127.0.0.1:{server.server_port}", flush=True)
    print("Press Ctrl+C to stop the server and any active search.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import JobManager, LocalServer
from optimize import maximum_power, result_document
from simulator import BOARD, FIXED_TILES, Simulation


def empty_board():
    return [
        [tile if (row, column) in FIXED_TILES else "EMPTY"
         for column, tile in enumerate(tiles)]
        for row, tiles in enumerate(BOARD)
    ]


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.server = LocalServer(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, document=None, headers=None, raw=None):
        data = raw if raw is not None else (
            json.dumps(document).encode() if document is not None else None
        )
        request = Request(self.url + path, data=data, headers={
            **({"Content-Type": "application/json"} if data is not None else {}),
            **(headers or {}),
        })
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            if response.headers.get_content_type() == "image/png":
                return response.status, payload
            body = payload.decode()
            return response.status, (
                json.loads(body) if response.headers.get_content_type() == "application/json" else body
            )

    def wait_for(self, job_id, statuses):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, job = self.request(f"/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            if job["status"] in statuses:
                return job
            time.sleep(0.02)
        self.fail(f"Search {job_id} did not reach {statuses}")

    def test_editor_assets_and_fixed_map_configuration(self):
        status, config = self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(len(config["fixed"]), 10)
        self.assertEqual(len(config["tiles"]), 7)
        for path, content in (("/", "Kaiju Cat Planner"), ("/app.js", "renderReplay"), ("/style.css", ".board")):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn(content, body)

    def test_command_icons_serve_the_supplied_pngs_only(self):
        icons = Path(__file__).with_name("icons")
        for name in ("north", "east", "south", "west", "stomp", "power_up"):
            with self.subTest(icon=name):
                status, content = self.request(f"/icons/{name}.png")
                self.assertEqual(status, 200)
                self.assertIsInstance(content, bytes)
                self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(content, (icons / f"{name}.png").read_bytes())
        for path in ("/icons/none.png", "/icons/missing.png", "/icons/../colors.txt", "/colors.txt"):
            self.assertEqual(self.request(path)[0], 404)

    def test_missing_icon_returns_not_found_without_exposing_other_files(self):
        with tempfile.TemporaryDirectory() as directory, patch("app.ICONS", Path(directory)):
            status, response = self.request("/icons/north.png")
        self.assertEqual(status, 404)
        self.assertEqual(response["error"], "Not found")

    def test_real_solver_result_contains_map_commands_and_replay(self):
        board = empty_board()
        status, response = self.request("/api/solve", {"map": board})
        self.assertEqual(status, 202)
        job = self.wait_for(response["id"], {"completed", "failed"})
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertTrue(job["result"]["optimal"])
        self.assertEqual(job["result"]["map"], board)
        self.assertEqual(job["result"]["score"], 0)
        self.assertEqual(job["result"]["upper_bound"], 0)
        self.assertEqual(len(job["result"]["trace"]), 15)

    def test_invalid_maps_limits_and_request_sizes_are_rejected(self):
        board = empty_board()
        board[0][0] = "EMPTY"
        status, response = self.request("/api/solve", {"map": board})
        self.assertEqual(status, 400)
        self.assertIn("must remain", response["error"])
        for seconds in (True, -1, 0, float("nan"), 3601):
            with self.subTest(seconds=seconds):
                status, _ = self.request("/api/solve", {"map": empty_board(), "seconds": seconds})
                self.assertEqual(status, 400)
        self.assertEqual(self.request("/api/solve", raw=b"x" * 65537)[0], 413)
        self.assertEqual(self.request("/api/solve", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertFalse(self.server.jobs.jobs)

    def test_only_one_search_runs_and_cancellation_preserves_best_result(self):
        def wait_until_cancelled(game_map, *, cancel_event, on_progress, **kwargs):
            simulation = Simulation(game_map=game_map)
            simulation.run()
            result = result_document(simulation, "FEASIBLE", maximum_power(game_map), 0)
            on_progress({"phase": "solving", "result": result, "upper_bound": result["upper_bound"]})
            cancel_event.wait(5)
            result["termination"] = "cancelled"
            return result

        self.server.jobs.runner = wait_until_cancelled
        status, response = self.request("/api/solve", {"map": empty_board()})
        self.assertEqual(status, 202)
        job_id = response["id"]
        self.wait_for(job_id, {"solving"})
        self.assertEqual(self.request("/api/solve", {"map": empty_board()})[0], 409)
        self.assertEqual(self.request(f"/api/jobs/{job_id}/cancel", {})[0], 202)
        job = self.wait_for(job_id, {"cancelled"})
        self.assertFalse(job["result"]["optimal"])
        self.assertEqual(job["result"]["termination"], "cancelled")
        self.assertEqual(job["result"]["score"], 0)

    def test_foreign_origins_hosts_and_arbitrary_files_are_not_served(self):
        self.assertEqual(self.request("/api/config", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.request(
            "/api/solve", {"map": empty_board()}, {"Origin": "https://example.com"}
        )[0], 403)
        self.assertEqual(self.request("/../simulator.py")[0], 404)

    def test_worker_failure_is_visible_without_an_optimality_claim(self):
        def fail(*args, **kwargs):
            raise ValueError("Solver could not build this model")

        self.server.jobs = JobManager(fail)
        status, response = self.request("/api/solve", {"map": empty_board()})
        self.assertEqual(status, 202)
        job = self.wait_for(response["id"], {"failed"})
        self.assertIn("could not build", job["error"])
        self.assertIsNone(job["result"])


if __name__ == "__main__":
    unittest.main()

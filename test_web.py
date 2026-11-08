"""Optional browser regressions; requires Playwright and Microsoft Edge."""

import json
import re
import threading
import unittest
from pathlib import Path

try:
    from playwright.sync_api import Error as BrowserError, expect, sync_playwright
except ImportError:
    sync_playwright = None

from app import JobManager, LocalServer
from optimize import maximum_power, result_document
from simulator import FIXED_TILES, GameMap, Simulation


PLACEMENTS = {
    (0, 1, 2): "NORTH",
    (0, 1, 1): "SOUTH",
    (0, 3, 2): "EAST",
    (0, 3, 1): "WEST",
    (0, 4, 2): "STOMP",
    (0, 4, 1): "POWER_UP",
    (0, 5, 1): "POWER_UP",
}
TILE_COLORS = {
    "RED_CAT": "ca3146", "GREEN_CAT": "6fe8bf", "BLUE_CAT": "15a6f4",
    "HIGH": "d7c37a", "LOW": "95a0b3", "EMPTY": "76c18d",
    "SPIKE": "2a4573", "POWER": "954ef3", "BOULDER": "40495c", "MUD": "644d5c",
}


def rgb(hex_color):
    channels = [int(hex_color[offset:offset + 2], 16) for offset in (0, 2, 4)]
    return f"rgb({channels[0]}, {channels[1]}, {channels[2]})"


def luminance(color):
    channels = [int(channel) / 255 for channel in re.findall(r"\d+", color)[:3]]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def document(placements, game_map=None):
    game_map = game_map if game_map is not None else GameMap()
    simulation = Simulation(placements, game_map)
    simulation.run()
    return result_document(simulation, "FEASIBLE", maximum_power(game_map), 0.1, "time_limit")


@unittest.skipUnless(sync_playwright, "Browser tests require the optional playwright package")
class CommandMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)
        except BrowserError as error:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Microsoft Edge is required: {error}") from error
        cls.server = LocalServer(0, JobManager(lambda game_map, **kwargs: document(PLACEMENTS, game_map)))
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1200, "height": 900})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.url)
        expect(self.page.locator("#solve")).to_be_enabled()
        self.page.locator("#solve").click()
        expect(self.page.locator("#placement-grid")).to_be_visible()
        expect(self.page.locator("#status")).to_contain_text("Time limit reached")
        self.wait_for_icons()

    def tearDown(self):
        try:
            self.assertEqual(self.errors, [])
        finally:
            self.context.close()

    def cell(self, row, column):
        return self.page.locator(
            f'#placement-grid [data-row="{row}"][data-column="{column}"]'
        )

    def render(self, placements, game_map=None):
        self.page.evaluate("(result) => renderResult(result)", document(placements, game_map))
        self.wait_for_icons()

    def wait_for_icons(self):
        self.page.wait_for_function(
            "[...document.images].every(image => image.complete && image.naturalWidth > 0)"
        )

    def test_commands_match_exact_tiles_and_floors(self):
        expect(self.page.locator("#placement-grid .tile")).to_have_count(35)
        expect(self.page.locator("#placement-grid .command-slot")).to_have_count(31)
        expect(self.page.locator("#placement-grid .assigned")).to_have_count(len(PLACEMENTS))
        for (row, column, floor), command in PLACEMENTS.items():
            slot = self.cell(row, column).locator(f'[data-floor="{floor}"]')
            expect(slot).to_have_attribute("data-command", command)
            expect(slot.locator("img.command-icon")).to_have_attribute("src", f"/icons/{command.lower()}.png")
            expect(slot.locator("img.command-icon")).to_have_attribute("alt", "")
            self.assertIn(f"Row {row + 1}, column {column + 1}", slot.get_attribute("aria-label"))
        self.assertEqual(
            set(self.page.locator(".placement-legend img").evaluate_all(
                "(icons) => icons.map(icon => icon.getAttribute('src'))"
            )),
            {f"/icons/{name}.png" for name in ("north", "east", "south", "west", "stomp", "power_up")},
        )
        self.assertEqual(
            self.cell(0, 1).locator(".command-slot").evaluate_all(
                "(slots) => slots.map(slot => Number(slot.dataset.floor))"
            ),
            [2, 1],
        )
        expect(self.cell(0, 5).locator(".command-slot")).to_have_count(1)
        expect(self.cell(0, 5).locator(".slot-floor")).to_have_text("Only (1)")
        expect(self.cell(2, 1).locator(".command-slot[data-command='NONE']")).to_have_count(2)
        for row, column in ((0, 0), (0, 2), (0, 6), (1, 3), (2, 3), (3, 2), (1, 6)):
            expect(self.cell(row, column).locator(".command-slot")).to_have_count(0)

        self.assertFalse(self.page.locator("#command-table").evaluate("(table) => table.open"))
        self.page.locator("#command-table summary").click()
        expect(self.page.locator("#commands")).to_be_visible()
        expect(self.page.locator("#commands tr").first).to_have_text("1, 2High buildingNorthSouth")
        self.page.locator("#turn").focus()
        self.page.locator("#turn").press("Home")
        expect(self.cell(0, 4).locator('[data-floor="2"]')).to_have_attribute("data-command", "STOMP")
        self.page.locator("#turn").press("End")
        expect(self.page.locator("#placement-grid .assigned")).to_have_count(len(PLACEMENTS))
        with self.page.expect_download() as downloaded:
            self.page.locator("#download").click()
        saved = json.loads(Path(downloaded.value.path()).read_text())
        self.assertEqual(
            {(item["row"] - 1, item["column"] - 1, item["floor"]): item["command"]
             for item in saved["commands"]},
            PLACEMENTS,
        )

    def test_new_results_clear_old_markers_and_use_the_result_map(self):
        self.render({(0, 1, 2): "WEST"})
        expect(self.page.locator("#placement-grid .assigned")).to_have_count(1)
        expect(self.cell(0, 1).locator('[data-floor="2"] img')).to_have_attribute("src", "/icons/west.png")
        expect(self.cell(0, 1).locator('[data-floor="1"]')).to_have_attribute("data-command", "NONE")
        self.render({})
        expect(self.page.locator("#placement-grid .assigned")).to_have_count(0)
        expect(self.page.locator("#placement-count")).to_contain_text("No commands required")
        board = GameMap().to_list()
        board[0][1] = "POWER"
        self.render({}, GameMap(board))
        expect(self.cell(0, 1)).to_have_class("tile tile-power has-command-slots")
        expect(self.cell(0, 1).locator(".command-slot")).to_have_count(1)
        expect(self.page.locator("#placement-grid .command-slot")).to_have_count(30)
        empty = GameMap([
            [tile if (row, column) in FIXED_TILES else "EMPTY"
             for column, tile in enumerate(tiles)]
            for row, tiles in enumerate(GameMap().board)
        ])
        self.render({}, empty)
        expect(self.page.locator("#placement-grid .command-slot")).to_have_count(0)
        self.render({})
        expect(self.page.locator("#placement-grid .command-slot")).to_have_count(31)

    def test_small_screens_keep_commands_inside_their_slots(self):
        for width in (1200, 390, 320):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                self.assertTrue(self.page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                ))
                overflow = self.page.locator("#placement-grid .command-slot").evaluate_all(
                    """slots => slots.flatMap(slot => {
                      const bounds = slot.getBoundingClientRect();
                      return [...slot.querySelectorAll('.slot-floor, .command-icon, .command-name')]
                        .filter(child => {
                          const box = child.getBoundingClientRect();
                          return box.left < bounds.left - 1 || box.right > bounds.right + 1
                            || box.top < bounds.top - 1 || box.bottom > bounds.bottom + 1
                            || child.scrollWidth > child.clientWidth + 1
                            || child.scrollHeight > child.clientHeight + 1;
                        }).map(child => child.textContent);
                    })"""
                )
                self.assertEqual(overflow, [])
                expect(self.cell(0, 4).locator('[data-floor="2"] .command-name')).to_have_text("Stomp")
                expect(self.cell(0, 5).locator('[data-floor="1"] .command-name')).to_have_text("Power Up")

    def test_palette_colors_survive_hover_disabled_states_and_replay(self):
        def check_grid(grid):
            cells = self.page.locator(f"#{grid} .tile").evaluate_all(
                """cells => cells.map(cell => {
                  const style = getComputedStyle(cell);
                  return {row: Number(cell.dataset.row), column: Number(cell.dataset.column),
                    background: style.backgroundColor, foreground: style.color, opacity: style.opacity};
                })"""
            )
            for cell in cells:
                tile = GameMap().board[cell["row"]][cell["column"]].replace("_BED", "_CAT")
                if tile == "OOB":
                    continue
                self.assertEqual(cell["background"], rgb(TILE_COLORS[tile]), (grid, tile))
                self.assertEqual(cell["opacity"], "1", (grid, tile))
                light, dark = sorted((luminance(cell["background"]), luminance(cell["foreground"])), reverse=True)
                self.assertGreaterEqual((light + 0.05) / (dark + 0.05), 4.5, (grid, tile))

        check_grid("editor-grid")
        check_grid("placement-grid")
        for tile in ("HIGH", "LOW", "EMPTY", "SPIKE", "POWER", "BOULDER", "MUD"):
            button = self.page.locator(f'#palette [data-tile="{tile}"]')
            button.hover()
            expect(button).to_have_css("background-color", rgb(TILE_COLORS[tile]))
        for row, column in ((0, 1), (0, 5)):
            cell = self.page.locator(f'#editor-grid [data-row="{row}"][data-column="{column}"]')
            cell.hover()
            expect(cell).to_have_css("background-color", rgb(TILE_COLORS[GameMap().board[row][column]]))
        self.page.evaluate("setBusy(true)")
        check_grid("editor-grid")
        for tile in ("HIGH", "LOW", "EMPTY", "SPIKE", "POWER", "BOULDER", "MUD"):
            button = self.page.locator(f'#palette [data-tile="{tile}"]')
            expect(button).to_have_css("background-color", rgb(TILE_COLORS[tile]))
            expect(button).to_have_css("opacity", "1")
        self.page.evaluate("setBusy(false)")
        self.page.locator("#turn").focus()
        self.page.locator("#turn").press("Home")
        check_grid("replay-grid")
        for color in ("red", "green", "blue"):
            expect(self.page.locator(f"#replay-grid .cat-marker.{color}")).to_have_css(
                "background-color", rgb(TILE_COLORS[f"{color.upper()}_CAT"])
            )
        self.page.locator("#turn").press("ArrowRight")
        self.page.locator("#turn").press("ArrowRight")
        expect(self.page.locator("#turn-label")).to_have_text("2")
        cleared = self.page.locator('#replay-grid [data-row="0"][data-column="1"]')
        self.assertTrue(cleared.evaluate("(cell) => cell.classList.contains('depleted')"))
        expect(cleared).to_have_css("background-color", rgb(TILE_COLORS["EMPTY"]))


if __name__ == "__main__":
    unittest.main()

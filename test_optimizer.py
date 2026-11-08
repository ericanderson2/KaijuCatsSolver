import unittest
import threading

from ortools.sat.python import cp_model

from optimize import Optimizer, TURNS, solve_map
from relaxation import power_limits
from simulator import (
    BOARD, COLORS, COMMANDS, DIRECTIONS, FIXED_TILES, GameMap, SCALE, Simulation,
)


ORIGINAL_PLAN = {
    (0, 1, 2): "NORTH",
    (0, 4, 2): "EAST",
    (0, 5, 1): "SOUTH",
    (1, 1, 2): "EAST",
    (1, 2, 2): "WEST",
    (2, 1, 2): "NORTH",
    (2, 5, 1): "EAST",
    (3, 3, 1): "EAST",
    (4, 1, 1): "EAST",
    (4, 3, 1): "SOUTH",
    (4, 4, 2): "POWER_UP",
    (4, 4, 1): "NORTH",
    (4, 5, 2): "POWER_UP",
    (4, 5, 1): "POWER_UP",
}


def assert_model_matches(test, optimizer, placements):
    model = optimizer.model.clone()
    model.clear_objective()
    simulation = Simulation(placements, optimizer.game_map)
    for turn in range(TURNS):
        floors = dict(simulation.floors)
        headings = {}
        for color, cat in simulation.cats.items():
            heading = cat.direction
            dr, dc = DIRECTIONS[heading]
            target = (cat.position[0] + dr, cat.position[1] + dc)
            if not cat.waiting and not optimizer.game_map.can_enter(color, target):
                heading = (heading + 2) % 4
            headings[color] = heading
        simulation.step()
        for color in COLORS:
            cat = simulation.cats[color]
            floor = floors.get(cat.position, 0)
            command = placements.get((*cat.position, floor), "NONE") if cat.alive and floor else "NONE"
            action = COMMANDS.index(command)
            if 1 <= action <= 4 and action - 1 == headings[color]:
                action = 0
            model.add(optimizer.action[color][turn] == action)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    test.assertEqual(status, cp_model.OPTIMAL, solver.status_name(status))
    _, replay = optimizer.replay(solver.value)
    test.assertEqual(replay.snapshot(), simulation.snapshot())
    test.assertEqual(replay.score, simulation.score)


def filled_map(tile):
    return GameMap([
        [original if (row, column) in FIXED_TILES else tile
         for column, original in enumerate(tiles)]
        for row, tiles in enumerate(BOARD)
    ])


class OptimizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.optimizer = Optimizer()

    def test_fixed_actions_match_independent_simulation(self):
        plans = {
            "no_commands": {},
            "fight_before_power_up": {
                (0, 1, 2): "SOUTH",
                (2, 1, 2): "NORTH",
                (1, 1, 2): "POWER_UP",
            },
            "rebound_and_stomp": {
                (0, 1, 2): "NORTH",
                (0, 1, 1): "POWER_UP",
                (4, 3, 2): "STOMP",
                (4, 3, 1): "NORTH",
                (3, 3, 2): "POWER_UP",
                (3, 3, 1): "EAST",
            },
            "mud_and_spike": {
                (0, 3, 2): "SOUTH",
                (4, 3, 2): "NORTH",
                (3, 3, 2): "WEST",
                (3, 4, 1): "POWER_UP",
            },
            "optimal_placement": ORIGINAL_PLAN,
        }
        for name, placements in plans.items():
            with self.subTest(plan=name):
                assert_model_matches(self, self.optimizer, placements)

    def test_optimal_placement_score_and_arrival_order(self):
        simulation = Simulation(ORIGINAL_PLAN)
        simulation.run(TURNS)
        self.assertEqual(simulation.cost, 200)
        self.assertEqual(simulation.score, 223750)
        self.assertEqual(
            [(color, simulation.cats[color].bed_turn, simulation.cats[color].power)
             for color in ("red", "blue", "green")],
            [("red", 10, 3250), ("blue", 14, 10500), ("green", 15, 210000)],
        )


class ConfigurableOptimizerTests(unittest.TestCase):
    def test_mud_rebound_delay_matches_in_simulation_model_and_bounds(self):
        board = filled_map("EMPTY").to_list()
        board[0][1], board[0][2], board[0][3] = "HIGH", "MUD", "BOULDER"
        game_map = GameMap(board)
        assert_model_matches(self, Optimizer(game_map), {})
        limits = power_limits("blue", game_map)
        # With no commands, the relaxed cat visits the high building on turns 1, 6, 9, 14.
        self.assertEqual(limits[0][0][0], 2000 * SCALE)

    def test_relocated_and_multiple_obstacles_match_simulation(self):
        board = filled_map("EMPTY").to_list()
        for (row, column), tile in {
            (0, 1): "HIGH", (0, 2): "SPIKE", (0, 3): "POWER",
            (0, 4): "SPIKE", (0, 5): "POWER", (2, 1): "HIGH",
            (2, 2): "SPIKE", (2, 3): "LOW", (1, 3): "HIGH",
            (4, 1): "MUD", (4, 2): "BOULDER",
        }.items():
            board[row][column] = tile
        assert_model_matches(self, Optimizer(GameMap(board)), {
            (0, 1, 2): "POWER_UP", (2, 1, 2): "POWER_UP",
        })

    def test_no_command_slots_proves_zero(self):
        result = solve_map(filled_map("EMPTY"), seconds=5, workers=1)
        self.assertTrue(result["optimal"])
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["upper_bound"], 0)
        self.assertEqual(result["commands"], [])

    def test_many_plants_do_not_require_exponential_resource_tables(self):
        optimizer = Optimizer(filled_map("POWER"))
        self.assertFalse(optimizer.exact_resources)
        self.assertEqual(optimizer.max_resource, TURNS)
        assert_model_matches(self, optimizer, {
            (0, 1, 1): "POWER_UP", (2, 1, 1): "POWER_UP",
            (4, 1, 1): "POWER_UP", (0, 3, 1): "SOUTH",
        })

    def test_exact_and_counted_plant_bounds_preserve_the_same_routes(self):
        for count in (4, 5):
            with self.subTest(plants=count):
                board = filled_map("EMPTY").to_list()
                board[0][1] = "HIGH"
                for column in range(2, 6):
                    board[0][column] = "POWER"
                placements = {(0, 1, 2): "POWER_UP", (0, 5, 1): "SOUTH"}
                if count == 5:
                    board[1][5] = "POWER"
                    placements[(1, 5, 1)] = "POWER_UP"
                assert_model_matches(self, Optimizer(GameMap(board)), placements)

    def test_small_map_power_caps_preserve_valid_routes(self):
        board = filled_map("EMPTY").to_list()
        board[0][1] = "HIGH"
        assert_model_matches(self, Optimizer(GameMap(board)), {
            (0, 1, 2): "POWER_UP", (0, 1, 1): "POWER_UP",
        })

    def test_cancelled_and_timed_out_searches_do_not_claim_optimality(self):
        cancelled = threading.Event()
        cancelled.set()
        result = solve_map(cancel_event=cancelled)
        self.assertFalse(result["optimal"])
        self.assertEqual(result["termination"], "cancelled")
        result = solve_map(seconds=0.000001, workers=1)
        self.assertFalse(result["optimal"])
        self.assertEqual(result["termination"], "time_limit")
        self.assertEqual(result["score"], Simulation().run()["score"])

    def test_original_optimum_can_still_be_proved(self):
        result = solve_map(
            seconds=60,
            hint=ORIGINAL_PLAN,
        )
        self.assertTrue(result["optimal"])
        self.assertEqual(result["score"], 223750)
        self.assertEqual(result["upper_bound"], result["score"])


if __name__ == "__main__":
    unittest.main()

import unittest
from fractions import Fraction

from simulator import BEDS, BOARD, FIXED_TILES, GameMap, INITIAL_FLOORS, Simulation


class MapTests(unittest.TestCase):
    def test_tile_counts_can_vary_without_changing_the_default_map(self):
        board = [list(row) for row in BOARD]
        for row in range(5):
            for column in range(7):
                if (row, column) not in FIXED_TILES:
                    board[row][column] = "POWER"
        game_map = GameMap(board)
        self.assertEqual(len(game_map.plants), 25)
        self.assertEqual(sum(game_map.floors.values()), 25)
        board[0][1] = "EMPTY"
        self.assertEqual(game_map.board[0][1], "POWER")
        self.assertEqual(GameMap().board[0][1], "HIGH")

    def test_dimensions_fixed_cells_and_tile_types_are_validated(self):
        with self.assertRaisesRegex(ValueError, "5 rows"):
            GameMap([])
        board = [list(row) for row in BOARD]
        board[0] = ["EMPTY"]
        with self.assertRaisesRegex(ValueError, "7 columns"):
            GameMap(board)
        for position in ((0, 0), (0, 6), (1, 6)):
            board = [list(row) for row in BOARD]
            board[position[0]][position[1]] = "EMPTY"
            with self.assertRaisesRegex(ValueError, "must remain"):
                GameMap(board)
        for tile in ("UNKNOWN", "OOB", "RED_CAT", None):
            board = [list(row) for row in BOARD]
            board[0][1] = tile
            with self.assertRaisesRegex(ValueError, "Invalid tile"):
                GameMap(board)

    def test_simulation_uses_relocated_obstacles_and_plants(self):
        board = [list(row) for row in BOARD]
        board[0][1], board[2][1], board[4][1] = "SPIKE", "BOULDER", "POWER"
        simulation = Simulation({(4, 1, 1): "POWER_UP"}, GameMap(board))
        simulation.cats["blue"].power = Fraction(3000)
        simulation.cats["green"].power = Fraction(500)
        simulation.step()
        self.assertEqual(simulation.cats["blue"].power, 1500)
        self.assertEqual(simulation.cats["red"].position, (2, 0))
        self.assertEqual(simulation.cats["red"].direction, 3)
        self.assertEqual(simulation.cats["green"].power, 2000)
        self.assertEqual(simulation.trace[0]["floors"][4][1], 0)

    def test_mud_rebound_schedules_exactly_one_more_wait(self):
        board = [list(row) for row in BOARD]
        board[0][1], board[0][2] = "MUD", "BOULDER"
        simulation = Simulation(game_map=GameMap(board))
        simulation.run(3)
        blue = simulation.cats["blue"]
        self.assertEqual(blue.position, (0, 1))
        self.assertEqual(blue.direction, 3)
        self.assertTrue(blue.waiting)
        simulation.step()
        self.assertEqual(blue.position, (0, 1))
        self.assertFalse(blue.waiting)
        simulation.step()
        self.assertEqual(blue.position, (0, 0))


class SimulationTests(unittest.TestCase):
    def test_initial_movement_and_floor_values(self):
        simulation = Simulation()
        simulation.step()
        self.assertEqual(simulation.cats["red"].power, 500)
        self.assertEqual(simulation.cats["green"].power, 250)
        self.assertEqual(simulation.cats["blue"].power, 500)
        self.assertEqual(simulation.score, 1250)

    def test_rebound_destroys_bottom_floor_and_executes_command(self):
        simulation = Simulation({(0, 1, 2): "NORTH", (0, 1, 1): "POWER_UP"})
        simulation.run(2)
        blue = simulation.cats["blue"]
        self.assertEqual(blue.position, (0, 1))
        self.assertEqual(blue.direction, 2)
        self.assertEqual(blue.power, 2000)
        self.assertEqual(simulation.floors[(0, 1)], 0)

    def test_stomp_waits_one_turn_and_executes_next_floor_command(self):
        simulation = Simulation({(0, 1, 2): "STOMP", (0, 1, 1): "WEST"})
        simulation.step()
        self.assertTrue(simulation.cats["blue"].waiting)
        simulation.step()
        self.assertFalse(simulation.cats["blue"].waiting)
        self.assertEqual(simulation.cats["blue"].position, (0, 1))
        self.assertEqual(simulation.cats["blue"].power, 1000)
        simulation.step()
        self.assertEqual(simulation.cats["blue"].position, (0, 0))

    def test_power_up_applies_after_doubling_and_plant_is_single_use(self):
        simulation = Simulation({(0, 5, 1): "POWER_UP"})
        blue = simulation.cats["blue"]
        blue.position = (0, 4)
        blue.power = Fraction(250)
        simulation.step()
        self.assertEqual(blue.power, 1500)
        simulation.step()
        self.assertEqual(blue.position, (0, 5))
        self.assertEqual(blue.power, 1500)
        self.assertEqual(blue.direction, 3)

    def test_fight_precedes_floor_effect_and_only_winner_uses_floor(self):
        simulation = Simulation({(2, 1, 2): "POWER_UP"})
        simulation.cats["red"].power = Fraction(250)
        blue = simulation.cats["blue"]
        blue.position, blue.direction, blue.power = (2, 2), 3, Fraction(1000)
        simulation.step()
        self.assertFalse(simulation.cats["red"].alive)
        self.assertEqual(simulation.cats["red"].power, 0)
        self.assertEqual(blue.power, 2500)
        self.assertEqual(simulation.floors[(2, 1)], 1)

    def test_three_way_tie_prefers_red(self):
        simulation = Simulation()
        simulation.cats["blue"].position, simulation.cats["blue"].direction = (2, 2), 3
        simulation.cats["green"].position, simulation.cats["green"].direction = (1, 1), 2
        simulation.step()
        self.assertTrue(simulation.cats["red"].alive)
        self.assertFalse(simulation.cats["green"].alive)
        self.assertFalse(simulation.cats["blue"].alive)
        self.assertEqual(simulation.cats["red"].power, 500)

    def test_green_beats_blue_on_tie(self):
        simulation = Simulation()
        simulation.cats["green"].position, simulation.cats["green"].direction = (4, 2), 3
        simulation.cats["blue"].position, simulation.cats["blue"].direction = (4, 0), 1
        simulation.step()
        self.assertTrue(simulation.cats["green"].alive)
        self.assertFalse(simulation.cats["blue"].alive)

    def test_swapping_tiles_does_not_cause_fight(self):
        simulation = Simulation()
        simulation.cats["green"].position, simulation.cats["green"].direction = (3, 1), 1
        simulation.cats["blue"].position, simulation.cats["blue"].direction = (3, 2), 3
        simulation.step()
        self.assertTrue(simulation.cats["green"].alive)
        self.assertTrue(simulation.cats["blue"].alive)
        self.assertEqual(simulation.cats["green"].position, (3, 2))
        self.assertEqual(simulation.cats["blue"].position, (3, 1))

    def test_simultaneous_beds_use_red_green_blue_order_and_stop_movement(self):
        simulation = Simulation()
        for color, power in (("red", 100), ("green", 200), ("blue", 300)):
            cat = simulation.cats[color]
            cat.position = (BEDS[color][0], 5)
            cat.power = Fraction(power)
        simulation.step()
        self.assertEqual([cat.power for cat in simulation.cats.values()], [2100, 600, 1500])
        simulation.run(3)
        self.assertEqual(simulation.score, 4200)
        self.assertTrue(all(cat.bed_turn == 1 for cat in simulation.cats.values()))

    def test_wrong_bed_rebounds_and_current_plant_still_triggers(self):
        simulation = Simulation()
        red = simulation.cats["red"]
        red.position, red.power = (2, 5), Fraction(125)
        simulation.step()
        self.assertEqual(red.position, (2, 5))
        self.assertEqual(red.direction, 3)
        self.assertEqual(red.power, 250)

    def test_mud_waits_exactly_one_turn(self):
        simulation = Simulation()
        green = simulation.cats["green"]
        green.position = (3, 1)
        simulation.step()
        self.assertEqual(green.position, (3, 2))
        self.assertTrue(green.waiting)
        simulation.step()
        self.assertEqual(green.position, (3, 2))
        self.assertFalse(green.waiting)
        simulation.step()
        self.assertEqual(green.position, (3, 3))
        self.assertEqual(green.power, 250)

    def test_spike_uses_exact_model_halves_including_on_boulder_rebound(self):
        simulation = Simulation()
        green = simulation.cats["green"]
        green.position, green.direction, green.power = (0, 3), 2, Fraction(125)
        simulation.step()
        self.assertEqual(green.power, Fraction(125, 2))
        simulation.step()
        self.assertEqual(green.position, (1, 3))
        self.assertEqual(green.direction, 0)
        self.assertEqual(green.power, Fraction(125, 4))

    def test_budget_and_command_slots_are_validated(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            Simulation({(*position, height): "POWER_UP" for position, height in list(INITIAL_FLOORS.items())[:7]})
        with self.assertRaisesRegex(ValueError, "No floor"):
            Simulation({(0, 2, 1): "NORTH"})
        with self.assertRaisesRegex(ValueError, "No floor"):
            Simulation({(0, 5, 2): "NORTH"})
        with self.assertRaisesRegex(ValueError, "Unknown command"):
            Simulation({(0, 1, 2): "FLY"})

    def test_non_bedded_cats_still_score(self):
        result = Simulation().run(1)
        self.assertEqual(result["score"], 1250)
        self.assertTrue(all(cat["bed_turn"] is None for cat in result["cats"].values()))


if __name__ == "__main__":
    unittest.main()

import argparse
import json
import math
import threading
import time
from fractions import Fraction
from pathlib import Path

from ortools.sat.python import cp_model

from relaxation import IMPOSSIBLE, check_cancel, plant_resources, power_limits, points, upper_bound
from simulator import (
    BEDS,
    BUDGET,
    COLORS,
    COLUMNS,
    COMMANDS,
    COSTS,
    DIRECTIONS,
    GameMap,
    ROWS,
    SCALE,
    STARTS,
    Simulation,
    TURNS,
    number,
    read_map,
    read_placements,
)


DEAD = ROWS * COLUMNS


def index(position):
    return position[0] * COLUMNS + position[1]


def maximum_power(game_map):
    floors = game_map.floors
    floor_points = sum(
        height * (500 if game_map.board[row][column] == "HIGH" else 250)
        for (row, column), height in floors.items()
        if game_map.board[row][column] != "POWER"
    )
    boosts = min(BUDGET // 30, sum(floors.values()))
    multiplier = 5 * 2 ** min(len(game_map.plants), TURNS)
    return ((floor_points + 1000 * boosts) * multiplier + 2000) * SCALE


class Optimizer:
    def __init__(self, game_map=None, cancel_event=None):
        self.game_map = game_map if game_map is not None else GameMap()
        self.cancel_event = cancel_event
        self.plants, self.max_resource, self.exact_resources = plant_resources(self.game_map)
        self.power_bound = maximum_power(self.game_map)
        self.global_bound = self.power_bound
        self.model = cp_model.CpModel()
        self.position = {}
        self.direction = {}
        self.waiting = {}
        self.power = {}
        self.alive = {}
        self.action = {}
        self.cost = []
        self.cat_costs = {color: [] for color in COLORS}
        self.plant_uses = {color: {plant: [] for plant in self.plants} for color in COLORS}
        self.bed_ranks = {color: [] for color in COLORS}
        self.floors = {
            position: [
                self.model.new_int_var(0, height, f"floors_{position}_{turn}")
                for turn in range(TURNS + 1)
            ]
            for position, height in self.game_map.floors.items()
        }
        for position, height in self.game_map.floors.items():
            self.model.add(self.floors[position][0] == height)

        for color in COLORS:
            locations = [
                index((row, column))
                for row in range(ROWS)
                for column in range(COLUMNS)
                if self.game_map.can_enter(color, (row, column))
            ] + [DEAD]
            self.position[color] = [
                self.model.new_int_var_from_domain(
                    cp_model.Domain.from_values(locations), f"position_{color}_{turn}"
                )
                for turn in range(TURNS + 1)
            ]
            self.direction[color] = [
                self.model.new_int_var(0, 3, f"direction_{color}_{turn}")
                for turn in range(TURNS + 1)
            ]
            self.waiting[color] = [
                self.model.new_bool_var(f"waiting_{color}_{turn}")
                for turn in range(TURNS + 1)
            ]
            self.power[color] = [
                self.model.new_int_var(0, self.power_bound, f"power_{color}_{turn}")
                for turn in range(TURNS + 1)
            ]
            self.alive[color] = [
                self.equal(self.position[color][turn], DEAD, f"dead_{color}_{turn}").Not()
                for turn in range(TURNS + 1)
            ]
            self.action[color] = []
            self.model.add(self.position[color][0] == index(STARTS[color]))
            self.model.add(self.direction[color][0] == 1)
            self.model.add(self.waiting[color][0] == 0)
            self.model.add(self.power[color][0] == 0)

        for turn in range(TURNS):
            check_cancel(cancel_event)
            self.add_turn(turn)
        self.model.add(sum(self.cost) <= BUDGET // 10)
        self.score = sum(self.power[color][-1] for color in COLORS)
        self.add_relaxed_bounds()
        self.model.maximize(self.score)

    def equal(self, expression, value, name):
        result = self.model.new_bool_var(name)
        self.model.add(expression == value).only_enforce_if(result)
        self.model.add(expression != value).only_enforce_if(result.Not())
        return result

    def conjunction(self, literals, name):
        result = self.model.new_bool_var(name)
        self.model.add_bool_and(literals).only_enforce_if(result)
        self.model.add_bool_or([literal.Not() for literal in literals]).only_enforce_if(result.Not())
        return result

    def movement_rows(self, color):
        rows = []
        bed = index(BEDS[color])
        for location in range(DEAD + 1):
            if location != DEAD and not self.game_map.can_enter(color, divmod(location, COLUMNS)):
                continue
            for direction, (dr, dc) in enumerate(DIRECTIONS):
                for waiting in (0, 1):
                    target, heading, mud, arrival = location, direction, 0, 0
                    if location == DEAD:
                        heading = 1
                    elif location != bed and not waiting:
                        row, column = divmod(location, COLUMNS)
                        destination = (row + dr, column + dc)
                        if self.game_map.can_enter(color, destination):
                            target = index(destination)
                            arrival = int(target == bed)
                        else:
                            heading = (direction + 2) % 4
                        target_row, target_column = divmod(target, COLUMNS)
                        mud = int(self.game_map.board[target_row][target_column] == "MUD")
                    rows.append((location, direction, waiting, target, heading, mud, arrival))
        return rows

    def add_turn(self, turn):
        model = self.model
        targets, headings, mud_waits, arrivals = {}, {}, {}, {}
        for color in COLORS:
            targets[color] = model.new_int_var(0, DEAD, f"target_{color}_{turn}")
            headings[color] = model.new_int_var(0, 3, f"heading_{color}_{turn}")
            mud_waits[color] = model.new_bool_var(f"mud_wait_{color}_{turn}")
            arrivals[color] = model.new_bool_var(f"arrival_{color}_{turn}")
            model.add_allowed_assignments(
                [
                    self.position[color][turn],
                    self.direction[color][turn],
                    self.waiting[color][turn],
                    targets[color],
                    headings[color],
                    mud_waits[color],
                    arrivals[color],
                ],
                self.movement_rows(color),
            )

        losses = {color: [] for color in COLORS}
        for first, color in enumerate(COLORS):
            for other in COLORS[first + 1 :]:
                same = self.equal(targets[color] - targets[other], 0, f"same_{color}_{other}_{turn}")
                stronger = model.new_bool_var(f"stronger_{color}_{other}_{turn}")
                model.add(self.power[color][turn] >= self.power[other][turn]).only_enforce_if(stronger)
                model.add(self.power[color][turn] < self.power[other][turn]).only_enforce_if(stronger.Not())
                losses[color].append(self.conjunction([same, stronger.Not()], f"loses_{color}_{other}_{turn}"))
                losses[other].append(self.conjunction([same, stronger], f"loses_{other}_{color}_{turn}"))
        for color in COLORS:
            survives = self.conjunction(
                [self.alive[color][turn], *[loss.Not() for loss in losses[color]]],
                f"survives_{color}_{turn}",
            )
            model.add(self.position[color][turn + 1] == targets[color]).only_enforce_if(survives)
            model.add(self.position[color][turn + 1] == DEAD).only_enforce_if(survives.Not())

        hits = {color: {"LOW": [], "HIGH": [], "POWER": []} for color in COLORS}
        for position, floors in self.floors.items():
            available = model.new_bool_var(f"available_{position}_{turn}")
            model.add(floors[turn] >= 1).only_enforce_if(available)
            model.add(floors[turn] == 0).only_enforce_if(available.Not())
            uses = []
            for color in COLORS:
                present = self.equal(
                    self.position[color][turn + 1], index(position), f"present_{color}_{position}_{turn}"
                )
                used = self.conjunction([present, available], f"used_{color}_{position}_{turn}")
                hits[color][self.game_map.board[position[0]][position[1]]].append(used)
                if position in self.plants:
                    self.plant_uses[color][position].append(used)
                uses.append(used)
            model.add(sum(uses) <= 1)
            model.add(floors[turn + 1] == floors[turn] - sum(uses))

        already_home = [
            self.equal(self.position[color][turn], index(BEDS[color]), f"home_{color}_{turn}")
            for color in COLORS
        ]
        for priority, color in enumerate(COLORS):
            low, high, plant = (
                self.equal(sum(hits[color][tile]), 1, f"{tile}_{color}_{turn}")
                for tile in ("LOW", "HIGH", "POWER")
            )
            destroyed = model.new_bool_var(f"destroyed_{color}_{turn}")
            model.add(destroyed == low + high + plant)
            action = model.new_int_var(0, 6, f"action_{color}_{turn}")
            self.action[color].append(action)
            model.add(action == 0).only_enforce_if(destroyed.Not())
            stomp = model.new_bool_var(f"stomp_{color}_{turn}")
            boost = model.new_bool_var(f"boost_{color}_{turn}")
            cost = model.new_int_var(0, 3, f"cost_{color}_{turn}")
            self.cost.append(cost)
            self.cat_costs[color].append(cost)
            action_rows = []
            for heading in range(4):
                for action_id, command in enumerate(COMMANDS):
                    # A paid turn to the existing heading is dominated by no command.
                    if 1 <= action_id <= 4 and action_id - 1 == heading:
                        continue
                    action_rows.append(
                        (
                            heading,
                            action_id,
                            action_id - 1 if 1 <= action_id <= 4 else heading,
                            int(command == "STOMP"),
                            int(command == "POWER_UP"),
                            COSTS[command] // 10,
                        )
                    )
            model.add_allowed_assignments(
                [headings[color], action, self.direction[color][turn + 1], stomp, boost, cost],
                action_rows,
            )
            alive = self.alive[color][turn + 1]
            model.add(self.waiting[color][turn + 1] == mud_waits[color] + stomp).only_enforce_if(alive)
            model.add(self.waiting[color][turn + 1] == 0).only_enforce_if(alive.Not())

            old_power, new_power = self.power[color][turn : turn + 2]
            spike = model.new_bool_var(f"spike_{color}_{turn}")
            model.add_element(
                self.position[color][turn + 1],
                [int(tile == "SPIKE") for row in self.game_map.board for tile in row] + [0],
                spike,
            )
            normal = model.new_bool_var(f"normal_{color}_{turn}")
            model.add(normal == alive - destroyed - spike - arrivals[color])
            model.add(new_power == 0).only_enforce_if(alive.Not())
            model.add(new_power == old_power).only_enforce_if(normal)
            model.add(new_power == old_power + (250 + 1000 * boost) * SCALE).only_enforce_if(low)
            model.add(new_power == old_power + (500 + 1000 * boost) * SCALE).only_enforce_if(high)
            model.add(new_power == 2 * old_power + 1000 * SCALE * boost).only_enforce_if(plant)
            model.add(2 * new_power == old_power).only_enforce_if(spike)
            earlier = sum(already_home) + sum(arrivals[other] for other in COLORS[:priority])
            for rank in range(3):
                order_matches = self.equal(earlier, rank, f"rank_{rank}_{color}_{turn}")
                arrives_at_rank = self.conjunction(
                    [arrivals[color], order_matches], f"bed_{rank}_{color}_{turn}"
                )
                self.bed_ranks[color].append((rank + 1) * arrives_at_rank)
                if rank == 0:
                    model.add(new_power == old_power + 2000 * SCALE).only_enforce_if(arrives_at_rank)
                else:
                    model.add(new_power == (2 * rank + 1) * old_power).only_enforce_if(arrives_at_rank)

    def add_relaxed_bounds(self):
        limits = {
            color: power_limits(color, self.game_map, self.cancel_event)
            for color in COLORS
        }
        bound, _ = upper_bound(limits, self.game_map, self.cancel_event)
        self.global_bound = min(self.power_bound, bound)
        self.model.add(self.score <= self.global_bound)
        for color in COLORS:
            mask = self.model.new_int_var(0, self.max_resource, f"plant_resource_{color}")
            spent = self.model.new_int_var(0, BUDGET // 10, f"spent_{color}")
            rank = self.model.new_int_var(0, 3, f"bed_rank_{color}")
            cap = self.model.new_int_var(0, self.power_bound, f"power_cap_{color}")
            self.model.add(mask == sum(
                self.plants[plant] * sum(uses) for plant, uses in self.plant_uses[color].items()
            ))
            self.model.add(spent == sum(self.cat_costs[color]))
            self.model.add(rank == sum(self.bed_ranks[color]))
            rows = []
            for plant_mask in range(self.max_resource + 1):
                for budget in range(BUDGET // 10 + 1):
                    for bed_rank in range(4):
                        raw = limits[color][bool(bed_rank)][plant_mask][budget]
                        if raw == IMPOSSIBLE:
                            if bed_rank:
                                continue
                            # A cat eliminated earlier need not complete a solo route.
                            raw = 0
                        rows.append((
                            plant_mask, budget, bed_rank,
                            min(self.power_bound, points(raw, bed_rank)),
                        ))
            self.model.add_allowed_assignments([mask, spent, rank, cap], rows)
            self.model.add(self.power[color][-1] <= cap)

    def placements(self, value):
        placements = {}
        for turn in range(TURNS):
            for color in COLORS:
                action = value(self.action[color][turn])
                if action:
                    position = divmod(value(self.position[color][turn + 1]), COLUMNS)
                    floor = value(self.floors[position][turn])
                    key = (*position, floor)
                    if key in placements:
                        raise AssertionError(f"Floor used twice: {key}")
                    placements[key] = COMMANDS[action]
        return placements

    def replay(self, value):
        placements = self.placements(value)
        simulation = Simulation(placements, self.game_map)
        for turn in range(TURNS):
            simulation.step()
            for color, cat in simulation.cats.items():
                assert cat.alive == bool(value(self.alive[color][turn + 1])), (turn, color, "alive")
                assert cat.power * SCALE == value(self.power[color][turn + 1]), (turn, color, "power")
                if cat.alive:
                    assert index(cat.position) == value(self.position[color][turn + 1]), (turn, color, "position")
                    assert cat.direction == value(self.direction[color][turn + 1]), (turn, color, "direction")
                    assert cat.waiting == bool(value(self.waiting[color][turn + 1])), (turn, color, "waiting")
            for position, floor in simulation.floors.items():
                assert floor == value(self.floors[position][turn + 1]), (turn, position, "floors")
        return placements, simulation

    def hint(self, placements):
        simulation = Simulation(placements, self.game_map)
        for turn in range(TURNS + 1):
            for color, cat in simulation.cats.items():
                self.model.add_hint(self.position[color][turn], index(cat.position) if cat.alive else DEAD)
                self.model.add_hint(self.power[color][turn], int(cat.power * SCALE))
                if cat.alive:
                    self.model.add_hint(self.direction[color][turn], cat.direction)
                self.model.add_hint(self.waiting[color][turn], cat.waiting)
            for position, floor in simulation.floors.items():
                self.model.add_hint(self.floors[position][turn], floor)
            if turn < TURNS:
                previous_floors = dict(simulation.floors)
                headings = {}
                for color, cat in simulation.cats.items():
                    heading = cat.direction
                    dr, dc = DIRECTIONS[heading]
                    target = (cat.position[0] + dr, cat.position[1] + dc)
                    if not cat.waiting and not self.game_map.can_enter(color, target):
                        heading = (heading + 2) % 4
                    headings[color] = heading
                simulation.step()
                for color, cat in simulation.cats.items():
                    floor = previous_floors.get(cat.position, 0)
                    command = placements.get((*cat.position, floor), "NONE") if cat.alive and floor else "NONE"
                    action = COMMANDS.index(command)
                    if 1 <= action <= 4 and action - 1 == headings[color]:
                        action = 0
                    self.model.add_hint(self.action[color][turn], action)


class Progress(cp_model.CpSolverSolutionCallback):
    def __init__(self, optimizer, on_solution, cancel_event):
        super().__init__()
        self.optimizer = optimizer
        self.on_solution = on_solution
        self.cancel_event = cancel_event

    def on_solution_callback(self):
        self.on_solution(self.value, self.best_objective_bound)
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.stop_search()


def result_document(simulation, status, bound, seconds, termination=None):
    return {
        "status": status,
        "optimal": status == "OPTIMAL",
        "termination": termination,
        "map": simulation.game_map.to_list(),
        "score": number(simulation.score),
        "upper_bound": number(Fraction(math.ceil(bound), SCALE)),
        "cost": simulation.cost,
        "seconds": round(seconds, 2),
        "coordinates": "Rows and columns are 1-based. Floor 2 is top; floor 1 is bottom.",
        "rules": {
            "starting_power": 0,
            "bed_and_tie_priority": list(COLORS),
            "fight_before_tile_effects": True,
            "rebound_applies_tile_effects": True,
            "mud_rebound_adds_wait": True,
            "spike_rebound_halves_power": True,
            "spike_halving": "exact",
            "spike_rounding_confirmed": False,
        },
        "commands": [
            {"row": row + 1, "column": column + 1, "floor": floor, "command": command}
            for (row, column, floor), command in sorted(
                simulation.placements.items(), key=lambda item: (item[0][0], item[0][1], -item[0][2])
            )
        ],
        "cats": simulation.snapshot(),
        "trace": simulation.trace,
    }


def solve_map(game_map=None, *, seconds=None, workers=4, seed=1, hint=None,
              on_progress=None, cancel_event=None, log=False):
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("The time limit must be positive, or None for no limit")
    if workers < 1:
        raise ValueError("At least one search worker is required")
    game_map = game_map if game_map is not None else GameMap()
    started = time.monotonic()
    best_simulation = Simulation(hint or {}, game_map)
    best_simulation.run()
    best_units = int(best_simulation.score * SCALE)
    bound = maximum_power(game_map)

    def publish(phase, include_result=False):
        if on_progress:
            on_progress({
                "phase": phase,
                "seconds": round(time.monotonic() - started, 2),
                "upper_bound": number(Fraction(bound, SCALE)),
                "result": result_document(
                    best_simulation, "FEASIBLE", bound, time.monotonic() - started
                ) if include_result else None,
            })

    def finish(status, termination):
        return result_document(
            best_simulation, status, bound, time.monotonic() - started, termination
        )

    publish("building", True)
    try:
        check_cancel(cancel_event)
        optimizer = Optimizer(game_map, cancel_event)
        bound = optimizer.global_bound
        optimizer.model.add(optimizer.score >= best_units)
        optimizer.hint(hint or {})
        error = optimizer.model.validate()
        if error:
            raise ValueError(error)
        check_cancel(cancel_event)
    except InterruptedError:
        return finish("FEASIBLE", "cancelled")

    solver = cp_model.CpSolver()
    if seconds is not None:
        solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = workers
    solver.parameters.random_seed = seed
    solver.parameters.log_search_progress = log
    solver.parameters.absolute_gap_limit = 0
    solver.parameters.relative_gap_limit = 0

    def update_bound(value):
        nonlocal bound
        candidate = math.ceil(value)
        if best_units <= candidate < bound:
            bound = candidate
            publish("solving")

    def on_solution(value, upper):
        nonlocal best_simulation, best_units
        score = value(optimizer.score)
        if score > best_units:
            _, best_simulation = optimizer.replay(value)
            best_units = score
            update_bound(upper)
            publish("solving", True)

    solver.best_bound_callback = update_bound
    done = threading.Event()

    def watch_cancellation():
        while not done.wait(0.1):
            if cancel_event is not None and cancel_event.is_set():
                solver.stop_search()

    watcher = threading.Thread(target=watch_cancellation, daemon=True)
    publish("solving", True)
    watcher.start()
    try:
        status = solver.solve(optimizer.model, Progress(optimizer, on_solution, cancel_event))
    finally:
        done.set()
        watcher.join()
    if status in (cp_model.MODEL_INVALID, cp_model.INFEASIBLE):
        raise RuntimeError(f"Solver failed: {solver.status_name(status)}")
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if solver.value(optimizer.score) >= best_units:
            _, best_simulation = optimizer.replay(solver.value)
            best_units = int(best_simulation.score * SCALE)
        update_bound(solver.best_objective_bound)
    if status == cp_model.OPTIMAL:
        bound = best_units
        return finish("OPTIMAL", "optimal")
    cancelled = cancel_event is not None and cancel_event.is_set()
    return finish("FEASIBLE", "cancelled" if cancelled else "time_limit")


def main():
    parser = argparse.ArgumentParser(description="Optimize the 15-turn Kaiju Cat simulation.")
    parser.add_argument("--map", type=Path, help="JSON grid, or a JSON document containing 'map'")
    parser.add_argument("--seconds", type=float, default=120, help="Search limit; 0 waits for a proof")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("solution.json"))
    parser.add_argument("--hint", type=Path)
    parser.add_argument("--log", action="store_true")
    args = parser.parse_args()

    def progress(event):
        if event["result"] is not None:
            print(
                f"{event['seconds']:.1f}s: score {event['result']['score']}, "
                f"bound {event['upper_bound']}", flush=True,
            )

    result = solve_map(
        read_map(args.map) if args.map else GameMap(),
        seconds=None if args.seconds == 0 else args.seconds,
        workers=args.workers, seed=args.seed,
        hint=read_placements(args.hint) if args.hint else None,
        on_progress=progress, log=args.log,
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        f"{result['status']}: {result['score']} points, ${result['cost']}, "
        f"upper bound {result['upper_bound']}; saved {args.output}",
        flush=True,
    )


if __name__ == "__main__":
    main()

"""Admissible score bounds: ignore fights and give buildings unlimited floors."""

import itertools
import time

from simulator import BEDS, BUDGET, COLORS, COLUMNS, DIRECTIONS, GameMap, ROWS, SCALE, STARTS, TURNS


IMPOSSIBLE = -10**18
BUDGET_UNITS = BUDGET // 10


def check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Search cancelled")


def plant_resources(game_map):
    # Counting plants instead of tracking their identities keeps large maps bounded.
    exact = len(game_map.plants) <= 4
    weights = {
        position: (1 << offset) if exact else 1
        for offset, position in enumerate(game_map.plants)
    }
    maximum = (1 << len(weights)) - 1 if exact else min(len(weights), TURNS)
    return weights, maximum, exact


def power_limits(color, game_map=None, cancel_event=None):
    game_map = game_map if game_map is not None else GameMap()
    plants, maximum, exact = plant_resources(game_map)
    # State: position, heading, waiting, plant resource, dollars / 10.
    states = {(*STARTS[color], 1, False, 0, 0): 0}
    limits = [
        [[IMPOSSIBLE] * (BUDGET_UNITS + 1) for _ in range(maximum + 1)]
        for _ in range(2)
    ]
    limits[0][0][0] = 0
    movements = {}
    for row in range(ROWS):
        for column in range(COLUMNS):
            if not game_map.can_enter(color, (row, column)):
                continue
            for heading, (dr, dc) in enumerate(DIRECTIONS):
                for waiting in (False, True):
                    target, direction, mud = (row, column), heading, False
                    if not waiting:
                        destination = (row + dr, column + dc)
                        if game_map.can_enter(color, destination):
                            target = destination
                        else:
                            direction = (heading + 2) % 4
                        mud = game_map.board[target[0]][target[1]] == "MUD"
                    movements[(row, column, heading, waiting)] = (*target, direction, mud)

    for _ in range(TURNS):
        check_cancel(cancel_event)
        following = {}
        for (row, column, heading, waiting, mask, spent), power in states.items():
            nr, nc, direction, mud = movements[(row, column, heading, waiting)]
            position = (nr, nc)
            tile = game_map.board[nr][nc]
            if position == BEDS[color]:
                limits[1][mask][spent] = max(limits[1][mask][spent], power)
                continue
            plant = plants.get(position, 0)
            if tile not in ("LOW", "HIGH"):
                # Skipping an unused plant relaxes another cat having consumed it.
                new_power = power // 2 if tile == "SPIKE" else power
                key = (nr, nc, direction, mud, mask, spent)
                if new_power > following.get(key, IMPOSSIBLE):
                    following[key] = new_power
                exhausted = (mask & plant) if exact else mask == maximum
                if not plant or exhausted:
                    continue
            new_mask = (mask | plant) if exact else mask + bool(plant)
            base = power * 2 if plant else power + (500 if tile == "HIGH" else 250) * SCALE
            for new_direction, delay, cost, bonus in (
                (direction, False, 0, 0),
                *((d, False, 1, 0) for d in range(4) if d != direction),
                (direction, True, 2, 0),
                (direction, False, 3, 1000 * SCALE),
            ):
                if spent + cost > BUDGET_UNITS:
                    continue
                key = (nr, nc, new_direction, delay, new_mask, spent + cost)
                new_power = base + bonus
                if new_power > following.get(key, IMPOSSIBLE):
                    following[key] = new_power
        states = following

    for (*_, mask, spent), power in states.items():
        limits[0][mask][spent] = max(limits[0][mask][spent], power)
    for home in range(2):
        for mask in range(maximum + 1):
            for budget in range(1, BUDGET_UNITS + 1):
                if limits[home][mask][budget - 1] > limits[home][mask][budget]:
                    limits[home][mask][budget] = limits[home][mask][budget - 1]
    return limits


def points(raw, rank):
    if raw == IMPOSSIBLE:
        return IMPOSSIBLE
    if rank == 1:
        return raw + 2000 * SCALE
    return raw * (2 * rank - 1) if rank else raw


def upper_bound(limits, game_map=None, cancel_event=None):
    game_map = game_map if game_map is not None else GameMap()
    plants, _, exact = plant_resources(game_map)
    best, allocation = IMPOSSIBLE, None
    plant_allocations = []
    if exact:
        for owners in itertools.product(range(4), repeat=len(plants)):
            masks = [0, 0, 0]
            for plant, owner in enumerate(owners):
                if owner < 3:
                    masks[owner] |= 1 << plant
            plant_allocations.append(masks)
    else:
        # A looser global bound avoids enumerating exponentially many allocations.
        plant_allocations.append(None)
    for count in range(4):
        for order in itertools.permutations(range(3), count):
            check_cancel(cancel_event)
            ranks = [0, 0, 0]
            for rank, cat in enumerate(order, 1):
                ranks[cat] = rank
            for masks in plant_allocations:
                scores = []
                for cat, (color, rank) in enumerate(zip(COLORS, ranks)):
                    raw = limits[color][bool(rank)][masks[cat]] if exact else [
                        max(resource[budget] for resource in limits[color][bool(rank)])
                        for budget in range(BUDGET_UNITS + 1)
                    ]
                    scores.append([points(value, rank) for value in raw])
                for red_budget in range(BUDGET_UNITS + 1):
                    red = scores[0][red_budget]
                    if red == IMPOSSIBLE:
                        continue
                    for green_budget in range(BUDGET_UNITS + 1 - red_budget):
                        blue_budget = BUDGET_UNITS - red_budget - green_budget
                        green, blue = scores[1][green_budget], scores[2][blue_budget]
                        if green == IMPOSSIBLE or blue == IMPOSSIBLE:
                            continue
                        score = red + green + blue
                        if score > best:
                            best = score
                            allocation = {
                                "ranks": dict(zip(COLORS, ranks)),
                                "plant_masks": dict(zip(COLORS, masks)) if exact else None,
                                "budgets": dict(zip(COLORS, (red_budget, green_budget, blue_budget))),
                                "scores": dict(zip(COLORS, (red / SCALE, green / SCALE, blue / SCALE))),
                            }
    return best, allocation


if __name__ == "__main__":
    started = time.monotonic()
    limits = {}
    for color in COLORS:
        limits[color] = power_limits(color)
        print(f"{color}: bounds computed ({time.monotonic() - started:.1f}s)", flush=True)
    bound, allocation = upper_bound(limits)
    print(f"Global upper bound: {bound / SCALE:g}")
    print(allocation)

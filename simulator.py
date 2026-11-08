import argparse
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


BOARD = (
    ("BLUE_CAT", "HIGH", "EMPTY", "LOW", "LOW", "POWER", "OOB"),
    ("OOB", "HIGH", "LOW", "SPIKE", "HIGH", "EMPTY", "RED_BED"),
    ("RED_CAT", "HIGH", "EMPTY", "BOULDER", "LOW", "POWER", "GREEN_BED"),
    ("OOB", "EMPTY", "MUD", "LOW", "POWER", "LOW", "BLUE_BED"),
    ("GREEN_CAT", "LOW", "EMPTY", "HIGH", "LOW", "LOW", "OOB"),
)
COLORS = ("red", "green", "blue")
STARTS = {"red": (2, 0), "green": (4, 0), "blue": (0, 0)}
BEDS = {"red": (1, 6), "green": (2, 6), "blue": (3, 6)}
DIRECTIONS = ((-1, 0), (0, 1), (1, 0), (0, -1))
DIRECTION_NAMES = ("NORTH", "EAST", "SOUTH", "WEST")
COMMANDS = ("NONE", *DIRECTION_NAMES, "STOMP", "POWER_UP")
COSTS = {"NONE": 0, **dict.fromkeys(DIRECTION_NAMES, 10), "STOMP": 20, "POWER_UP": 30}
HEIGHTS = {"HIGH": 2, "LOW": 2, "POWER": 1}
EDITABLE_TILES = ("HIGH", "LOW", "POWER", "MUD", "SPIKE", "BOULDER", "EMPTY")
ROWS, COLUMNS = len(BOARD), len(BOARD[0])
TURNS, BUDGET = 15, 200
# Preserve exact halves in the model; the game's rounding behavior is unconfirmed.
SCALE = 2**TURNS
FIXED_TILES = {
    (row, column): tile
    for row, tiles in enumerate(BOARD)
    for column, tile in enumerate(tiles)
    if tile not in EDITABLE_TILES
}
INITIAL_FLOORS = {
    (row, column): HEIGHTS[tile]
    for row, tiles in enumerate(BOARD)
    for column, tile in enumerate(tiles)
    if tile in HEIGHTS
}


@dataclass(frozen=True)
class GameMap:
    board: tuple = BOARD

    def __post_init__(self):
        if not isinstance(self.board, (list, tuple)) or len(self.board) != ROWS:
            raise ValueError(f"The map must have exactly {ROWS} rows")
        for row, tiles in enumerate(self.board):
            if not isinstance(tiles, (list, tuple)) or len(tiles) != COLUMNS:
                raise ValueError(f"Row {row + 1} must have exactly {COLUMNS} columns")
            for column, tile in enumerate(tiles):
                position = (row, column)
                if position in FIXED_TILES:
                    if tile != FIXED_TILES[position]:
                        raise ValueError(
                            f"Row {row + 1}, column {column + 1} must remain "
                            f"{FIXED_TILES[position]}"
                        )
                elif not isinstance(tile, str) or tile not in EDITABLE_TILES:
                    raise ValueError(f"Invalid tile at row {row + 1}, column {column + 1}")
        object.__setattr__(self, "board", tuple(tuple(row) for row in self.board))

    @property
    def floors(self):
        return {
            (row, column): HEIGHTS[tile]
            for row, tiles in enumerate(self.board)
            for column, tile in enumerate(tiles)
            if tile in HEIGHTS
        }

    @property
    def plants(self):
        return [
            (row, column)
            for row, tiles in enumerate(self.board)
            for column, tile in enumerate(tiles)
            if tile == "POWER"
        ]

    def can_enter(self, color, position):
        row, column = position
        if not (0 <= row < ROWS and 0 <= column < COLUMNS):
            return False
        tile = self.board[row][column]
        return tile not in ("OOB", "BOULDER") and (
            not tile.endswith("_BED") or position == BEDS[color]
        )

    def to_list(self):
        return [list(row) for row in self.board]


def number(value):
    value = Fraction(value)
    return value.numerator if value.denominator == 1 else str(value)


def can_enter(color, position):
    return GameMap().can_enter(color, position)


@dataclass
class Cat:
    position: tuple[int, int]
    direction: int = 1
    power: Fraction = Fraction(0)
    waiting: bool = False
    alive: bool = True
    bed_turn: int | None = None


class Simulation:
    def __init__(self, placements=None, game_map=None):
        self.game_map = game_map if game_map is not None else GameMap()
        self.floors = self.game_map.floors
        self.placements = dict(placements or {})
        for (row, column, floor), command in self.placements.items():
            if not 1 <= floor <= self.floors.get((row, column), 0):
                raise ValueError(f"No floor {floor} at row {row + 1}, column {column + 1}")
            if command not in COSTS:
                raise ValueError(f"Unknown command: {command}")
        self.cost = sum(COSTS[command] for command in self.placements.values())
        if self.cost > BUDGET:
            raise ValueError(f"Command cost ${self.cost} exceeds the ${BUDGET} budget")
        self.cats = {color: Cat(STARTS[color]) for color in COLORS}
        self.arrivals = 0
        self.turn = 0
        self.trace = []

    def step(self):
        self.turn += 1
        events = []
        for color, cat in self.cats.items():
            if not cat.alive or cat.bed_turn is not None:
                continue
            if cat.waiting:
                cat.waiting = False
            else:
                dr, dc = DIRECTIONS[cat.direction]
                target = (cat.position[0] + dr, cat.position[1] + dc)
                if self.game_map.can_enter(color, target):
                    cat.position = target
                else:
                    cat.direction = (cat.direction + 2) % 4
                    events.append(f"{color}: rebound")
                cat.waiting = self.game_map.board[cat.position[0]][cat.position[1]] == "MUD"

        occupants = {}
        for color, cat in self.cats.items():
            if cat.alive:
                occupants.setdefault(cat.position, []).append(color)
        for colors in occupants.values():
            if len(colors) < 2:
                continue
            winner = max(colors, key=lambda color: (self.cats[color].power, -COLORS.index(color)))
            for color in colors:
                if color != winner:
                    self.cats[color].alive = False
                    self.cats[color].power = Fraction(0)
                    self.cats[color].waiting = False
                    events.append(f"{winner} defeats {color}")

        # This iteration order also resolves simultaneous bed arrivals.
        for color, cat in self.cats.items():
            if not cat.alive or cat.bed_turn is not None:
                continue
            row, column = cat.position
            tile = self.game_map.board[row][column]
            floor = self.floors.get(cat.position, 0)
            if floor:
                self.floors[cat.position] -= 1
                if tile == "POWER":
                    cat.power *= 2
                else:
                    cat.power += 500 if tile == "HIGH" else 250
                command = self.placements.get((row, column, floor), "NONE")
                if command in DIRECTION_NAMES:
                    cat.direction = DIRECTION_NAMES.index(command)
                elif command == "STOMP":
                    cat.waiting = True
                elif command == "POWER_UP":
                    cat.power += 1000
                events.append(
                    f"{color}: ({row + 1},{column + 1}) floor {floor}, {tile}, {command}"
                )
            elif tile == "SPIKE":
                cat.power = Fraction(cat.power, 2)
                events.append(f"{color}: spike")
            elif cat.position == BEDS[color]:
                self.arrivals += 1
                cat.power = cat.power + 2000 if self.arrivals == 1 else cat.power * (2 * self.arrivals - 1)
                cat.bed_turn = self.turn
                events.append(f"{color}: bed arrival #{self.arrivals}")
        self.trace.append({
            "turn": self.turn,
            "cats": self.snapshot(),
            "floors": [
                [self.floors.get((row, column), 0) for column in range(COLUMNS)]
                for row in range(ROWS)
            ],
            "events": events,
        })

    def snapshot(self):
        return {
            color: {
                "row": cat.position[0] + 1,
                "column": cat.position[1] + 1,
                "direction": DIRECTION_NAMES[cat.direction],
                "power": number(cat.power),
                "waiting": cat.waiting,
                "alive": cat.alive,
                "bed_turn": cat.bed_turn,
            }
            for color, cat in self.cats.items()
        }

    @property
    def score(self):
        return sum((cat.power for cat in self.cats.values() if cat.alive), Fraction(0))

    def run(self, turns=TURNS):
        for _ in range(turns):
            self.step()
        return {
            "map": self.game_map.to_list(),
            "score": number(self.score),
            "cost": self.cost,
            "cats": self.snapshot(),
            "trace": self.trace,
        }


def read_map(path, allow_default=False):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(document, dict):
        if "map" not in document and not allow_default:
            raise ValueError("The JSON document must contain a 'map' grid")
        document = document.get("map", BOARD)
    return GameMap(document)


def read_placements(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    placements = {}
    for item in document["commands"]:
        key = (item["row"] - 1, item["column"] - 1, item["floor"])
        if key in placements:
            raise ValueError(f"Duplicate command slot: {key}")
        placements[key] = item["command"]
    return placements


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay a Kaiju Cat command placement.")
    parser.add_argument("placement", type=Path)
    parser.add_argument("--map", type=Path, help="Override the map embedded in the solution")
    parser.add_argument("--turns", type=int, default=TURNS)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    game_map = read_map(args.map) if args.map else read_map(args.placement, allow_default=True)
    result = Simulation(read_placements(args.placement), game_map).run(args.turns)
    if not args.trace:
        result.pop("trace")
    print(json.dumps(result, indent=2))

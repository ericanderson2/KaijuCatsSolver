# Game rules

This is the canonical maintainer reference for the behavior modeled by Kaiju
Cat Planner. It consolidates the original game description and subsequent
clarifications. The simulator, optimizer, bounds, and regression cases must
remain consistent with this contract.

**Known uncertainty:** the game's rounding behavior after a spike halves an
odd amount of power has not been confirmed. The planner currently keeps exact
fractions. This is a modeling assumption, not a confirmed game mechanic; see
[Power precision](#power-precision).

## Objective and initial state

- There are exactly three cats: Red, Green, and Blue.
- All cats begin alive, with **zero power**, facing **east**, and without a
  pending wait.
- A run lasts **15 turns**. Turn 1 includes the first movement; turn 0 is only
  the initial state.
- The objective is the sum of surviving cats' power after all turn-15 effects.
  Cats do **not** need to reach their beds to contribute to the score.
- Commands share a single **$200 budget** across the whole map, not per cat.
  Unspent money adds no power.
- Given a map and a placement, the simulation is deterministic. Map generation
  is outside the planner's scope.

## Board and coordinates

The board has **5 rows and 7 columns**. Coordinates in this document, the UI,
and exported command placements are **1-based `(row, column)`**, with `(1, 1)`
at the top left. Python board indices are 0-based.

North decreases the row, south increases it, east increases the column, and
west decreases it. Movement is orthogonal, never diagonal.

These positions are fixed:

| Cat | Starting tile | Matching bed |
|---|---|---|
| Blue | `(1, 1)` | `(4, 7)` |
| Red | `(3, 1)` | `(2, 7)` |
| Green | `(5, 1)` | `(3, 7)` |

The fixed in-grid `OOB` cells are `(1, 7)`, `(2, 1)`, `(4, 1)`, and `(5, 7)`.
Every coordinate outside the rectangle is also out of bounds.

The other **25 cells** can contain any of the editable tile types below.
Positions **and counts** may vary between maps, including zero instances of a
type. Do not assume the reference map's inventory or number of power plants.

Starting-tile labels (`BLUE_CAT`, `RED_CAT`, `GREEN_CAT`) identify initial
positions; they do not represent additional stationary cats. Those cells act
like empty terrain during play. Beds retain their color-specific behavior.

## Tiles and shared floors

| Tile | Floors / command slots | Effect |
|---|---|---|
| `HIGH` | 2 | Each destroyed floor adds **500** power. |
| `LOW` | 2 | Each destroyed floor adds **250** power. |
| `POWER` | 1 | Destroying its only floor **doubles** the acting cat's current power. |
| `MUD` | 0 | Entry or a rebound on this tile schedules a one-turn wait. |
| `SPIKE` | 0 | Halves the acting cat's current power, including on a rebound. |
| `BOULDER` | 0 | Blocks entry and causes a rebound. |
| `EMPTY` | 0 | No power, command, or movement effect. |
| Matching bed | 0 | Applies its arrival-order bonus once and stops the cat. |
| Wrong-color bed | 0 | Blocks entry and causes a rebound. |
| `OOB` | 0 | Blocks entry and causes a rebound. |

Buildings and plants are passable. All cats share the same remaining-floor
state: a floor destroyed by one cat is unavailable to every other cat.

- A two-floor building loses **floor 2 (top)** before **floor 1 (bottom)**.
  A plant has only floor 1.
- An acting cat destroys at most **one floor per turn**, then executes that
  floor's command, if any.
- Destroying the top floor does not automatically destroy the bottom floor or
  make the cat stay for another turn.
- A depleted building or plant behaves like empty terrain. Its old commands
  never execute again.
- Plants are single-use for the entire run, not once per cat.
- Mud, spikes, and boulders are not destroyed. They never have command slots.

`POWER` is a terrain type; `POWER_UP` is a command. They are different things.
A spike and a plant cannot occupy the same cell, and a Power Up command cannot
be attached directly to a spike.

## Turn resolution

Resolve each turn in this order:

1. **Movement or waiting.** Every alive cat that has not reached its bed either
   consumes its pending wait without moving, or attempts one step in its current
   direction. A blocked move leaves it on the current cell and reverses its
   direction. All cats' movement is simultaneous.
2. **Fights.** Resolve all groups of cats sharing an end position, using their
   power before this turn's tile effects. Remove losers before any floor is
   destroyed or command is executed.
3. **Tile effects.** Each remaining active cat applies the effect of its
   resulting tile: destroy one remaining floor, apply a spike penalty, or
   schedule a mud wait as appropriate.
4. **Floor commands.** Execute only the command on a floor actually destroyed
   this turn, after its normal points or plant multiplier. A command can replace
   a direction produced by a rebound.
5. **Bed arrivals.** Apply bonuses to newly arriving cats in arrival order,
   resolving same-turn arrivals by color priority, and stop those cats.

No command grants an additional movement in the same turn. Waiting and rebound
turns still count toward the 15-turn limit. Effects and bed bonuses on turn 15
count toward the final score.

Cats already in bed do not move or repeat their bed bonus. Eliminated cats do
not move, occupy cells, destroy floors, execute commands, or arrive at beds.

## Movement, rebounds, and delays

A move is blocked by the outside of the board, an in-grid `OOB` cell, a boulder,
or a bed of another color. The cat stays put for that turn and turns 180 degrees;
it does not immediately move in the reversed direction.

A rebound still triggers the current tile's applicable effect:

- On a building with a floor remaining, destroy that floor and execute its
  command.
- A previously destroyed plant does not double power again.
- On a spike, apply **another 50% penalty**.
- On mud, schedule **another one-turn wait**.

Mud entry and mud rebounds each cause exactly one skipped movement on the
following turn. Merely consuming that wait does **not** schedule another wait.
The cat keeps its direction during the waiting turn.

Stomp also schedules a wait, but the normal floor effect still occurs during
that stationary turn if a floor remains. Even a Stomp on a last floor or a
plant's only floor delays the cat for one turn; it cannot recreate a destroyed
floor or repeat a spent plant multiplier.

Waiting does not protect a cat from fights. Another cat can move onto its cell.

## Commands and spending

The placement is chosen before the run and remains fixed. Each physical floor
can have at most one command. Commands belong to floors, not to particular cats:
whichever surviving cat destroys the floor executes the command.

| Choice | Cost | Effect after the floor's normal effect |
|---|---|---|
| No command | $0 | Preserve the current direction and continue normally. |
| `NORTH`, `EAST`, `SOUTH`, `WEST` | $10 | Set the absolute direction for subsequent movement. |
| `STOMP` | $20 | Stay on this tile on the next turn and apply its remaining floor effect, if any. |
| `POWER_UP` | $30 | Add **1,000** power. |

The total purchase cost must be at most $200. A command still costs money if
its floor is never reached. There is no charge per execution and no refund for
an unused command.

A turn to the cat's current direction is a legal paid no-op. The optimizer may
omit such commands, comparing against the direction **after any rebound**.
Stomp on a last floor is not a no-op: its delay can change fights and bed order.

Power Up adds its bonus **after** a plant doubles the existing power. For
example, a cat with 1,000 power destroying a plant with Power Up finishes with
`1,000 * 2 + 1,000 = 3,000`, not 4,000. A later spike halves the accumulated
power normally.

## Fights

A fight occurs when cats finish movement on the same cell, including cats that
waited or rebounded. Merely swapping cells in the same turn does **not** cause
a fight.

The cat with the highest pre-tile-effect power wins. Ties use:

**Red > Green > Blue**

If three cats meet, choose one winner using the same ordering across all three.
There are no intermediate floor effects between opponents.

All losers are removed permanently and all of their points are lost. Their
power is not transferred to the winner. Only the winner receives that turn's
tile effect and, where applicable, destroys one floor and executes its command.

## Beds and scoring

Only a cat's matching bed is enterable. A matching bed stops the cat immediately
and awards exactly one bonus:

| Arrival number | Bonus |
|---|---|
| First | Add **2,000** power. |
| Second | Multiply current power by **3**. |
| Third | Multiply current power by **5**. |

Arrival numbers are shared across all three beds. Earlier turns take precedence;
if multiple cats arrive during the same turn, process them **Red, Green, Blue**.
This priority does not override arrivals on earlier turns.

The final score is the sum of every surviving cat's power after turn 15,
including cats still roaming. Reaching all beds, keeping every cat alive, and
spending the entire budget are not separate requirements for a valid solution.

## Power precision

**Confirmed:** spikes remove 50% of current power and repeat the penalty on a
rebound.

**Unconfirmed:** whether the actual game rounds fractional results, and if so,
how. A displayed whole number alone does not establish how the game compares
power internally.

Until that behavior is confirmed, the planner uses exact halves without
rounding. For example, its model turns 125 into `125/2`, then `125/4` after a
second spike hit. The simulator uses rational numbers; the optimizer uses
integer fixed-point units sufficient for 15 halvings.

This assumption affects power comparisons and fight ties, not just displayed
scores. Do not round a value for presentation and feed it back into the
simulation. An `OPTIMAL` result proves optimality under the modeled rules,
including this assumption.

## Timing examples

**Building rebound:** put a two-floor high building at `(1, 2)`, with North on
the top floor and Power Up on the bottom. Blue starts at `(1, 1)` facing east.
On turn 1, it enters the building, gains 500, and faces north. On turn 2, the
board edge causes a rebound south without movement; Blue destroys the bottom
floor and gains another 500 plus 1,000, finishing that turn with 2,000 power.

**Mud rebound:** put mud at `(1, 2)` and a boulder at `(1, 3)`. Blue enters mud on
turn 1 and waits on turn 2. On turn 3, it attempts to enter the boulder, remains
on the mud, and reverses west. It waits again on turn 4 and can leave west on
turn 5.

**Fight before effects:** Red and Blue each have 500 power and reach the same
high building. Red wins the tie before either receives that floor's 500 points.
Blue is eliminated; Red alone destroys the floor and then executes its command.

## Reference map

This is the original example, not a required tile inventory for other maps:

```json
{
  "map": [
    ["BLUE_CAT", "HIGH", "EMPTY", "LOW", "LOW", "POWER", "OOB"],
    ["OOB", "HIGH", "LOW", "SPIKE", "HIGH", "EMPTY", "RED_BED"],
    ["RED_CAT", "HIGH", "EMPTY", "BOULDER", "LOW", "POWER", "GREEN_BED"],
    ["OOB", "EMPTY", "MUD", "LOW", "POWER", "LOW", "BLUE_BED"],
    ["GREEN_CAT", "LOW", "EMPTY", "HIGH", "LOW", "LOW", "OOB"]
  ]
}
```

Its optimum under the exact-halving model is **223,750**. The `ORIGINAL_PLAN`
fixture in [test_optimizer.py](test_optimizer.py) preserves a reproducible
optimal placement without requiring a generated solution file. Other maps have
different optima, and optimal placements need not be unique.

## Maintaining the rule contract

When a mechanic changes or an assumption is resolved, keep these together:

- [simulator.py](simulator.py): map validation, state transitions, and scoring.
- [optimize.py](optimize.py): constraints, bounds integration, solution metadata,
  and independent replay of solver output.
- [relaxation.py](relaxation.py): admissible upper bounds. Relaxations may
  overestimate achievable power, but must never exclude a legal higher score.
- [test_simulator.py](test_simulator.py) and
  [test_optimizer.py](test_optimizer.py): focused rule and cross-model regressions.
- This document, the README, and any affected editor tooltips or result metadata.

The relaxed solver's unlimited building floors and simplified plant tracking
are bounding techniques, **not game rules**. The real simulation always uses
shared, consumable floors.

The optimizer may choose commands at destruction events internally, but its
output must be a static, budget-valid placement that reproduces every modeled
turn. A previous result's optimality proof is not transferable to a changed
map or changed rules.

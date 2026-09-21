# Compute Budget

Every bot plays under a **compute budget**: a limit on how much CPU your strategy can use over
a match. This page explains how the budget is measured, what happens when you run out, and
how to stay inside it.

## TL;DR

- You are charged for the **CPU time** your bot uses on each tick, not wall-clock time.
- You start with a bank of spare time and get a fixed allowance added back every tick.
  Roughly, that's **1–3ms of CPU per tick**, plus a **1–3 second bank** for occasional big
  computations.
- Spend more than you have and your bank goes **negative**. Until it's positive again, your
  bot **sits out ticks**: the engine doesn't call it, and your fleet does nothing.
- A single tick that takes more than **5 seconds** of real time forfeits your whole bank.
- **You can ask how much you have left.** `get_budget()` tells you your remaining bank and
  what your last tick cost — see [Asking the engine](#asking-the-engine).
- While developing, `mm-cli run --no-time-limit` turns enforcement off without turning the
  measurement off — see [Turning it off while you develop](#turning-it-off-while-you-develop).

## How the budget is measured

Each tick, the engine hands your bot the game state, and your strategy returns an action.
Everything your bot process does between receiving the state and returning the action is
measured as **CPU time**. That's the time the processor actually spends working for your
process, including every thread it runs.

CPU time isn't wall-clock time. Waiting doesn't cost anything: if your bot sleeps, it isn't
charged. It also isn't faster, and it can still hit the hang timeout below. Deliberately
slowing the match down this way is against the rules (see [Fair play](#fair-play)).

### Units: engine ticks

The budget isn't counted in milliseconds. It's counted in **engine ticks**: how long the
engine itself takes to simulate one tick of the game. The engine keeps a moving average of its
own per-tick CPU time (over the last 50 ticks) and divides your CPU time by it:

```
charge = your CPU time this tick / engine's average CPU time per tick
```

That way the budget scales with the machine: a slower computer runs both the engine and your
bot more slowly, and the charge comes out about the same.

One side effect: the engine gets more expensive as fleets grow, so an engine tick costs more
late in a match than early on. In practice an engine tick is a few microseconds. Early in a
match one budget tick is worth less real CPU than late in a match.

## The bank

| | engine ticks | roughly, in CPU time |
|---|---|---|
| Starting bank (also the maximum) | 500,000 | 1–3 seconds |
| Refill, every tick | 600 | 1.3–3.3 ms |

Every tick:

1. Your bot is charged for the CPU it used.
2. The refill is added.
3. The bank is capped at its maximum.

A bot that stays under the refill every tick keeps a full bank all match. The bank is there
for **bursts**: an expensive plan every few hundred ticks, a one-time analysis when something
changes. It isn't there to spend more than the refill every tick.

## Running out

The bank can go **negative**. If one tick uses far more than you have, the whole overspend is
kept as a debt.

While your bank is at or below zero:

- **Your bot isn't called.** The engine doesn't send it the state, and your code doesn't run.
- **Your fleet gets an empty action.** No bot moves, turns, fires, heals, or mines, and no
  rush order is placed. The fabricator keeps its natural build cadence, but it builds the
  default class (Battle).
- **Each skipped tick refills the bank** as usual, so you're back as soon as the debt is
  paid off.

Your bot isn't told that it missed ticks — it isn't running, so there's nothing to tell. The
next state you receive just has a later `tick` than the last one, so if you keep state between
ticks, compare `state.tick` with the previous value to notice the gap.

You *are* told your bank, on every tick you do run, so sitting out is avoidable rather than
something you only find out about afterwards. See below.

The cost adds up fast. A tick that uses one full second of CPU when an engine tick costs 4µs
is a charge of 250,000 engine ticks. Spend that with an empty bank and you sit out about 400
ticks.

## The hang timeout

Separately from the budget, each tick has a **5-second wall-clock** timeout. It only exists to
catch bots that are stuck (an infinite loop, a deadlock, a crash that didn't exit). If your
bot doesn't respond within 5 seconds:

- that tick's action is empty, and
- **your bank is emptied**, since the engine has no reliable CPU reading to charge you with.

Under the normal budget you should never come close to this.

## What isn't charged

- **Startup.** Launching your bot, imports, and module-level setup all happen before the
  handshake, which isn't charged. It has its own 10-second wall-clock limit.
- **The very first tick (tick 0).** The engine hasn't timed a tick of its own yet, so there's
  nothing to charge against. Tick 0 is still subject to the 5-second hang timeout.
- **Time spent waiting for the engine.** Only the stretch between receiving the state and
  returning your action counts.

## Asking the engine

`get_budget()` answers with what the engine is actually charging you, in the engine ticks of
the section above — you don't have to convert anything or guess at the engine's speed.

**Python**

```python
from core.channel import get_budget, COMPUTE_BANK_TICKS

def my_strategy(state):
    budget = get_budget()
    budget.remaining    # ticks left in the bank; negative means you're in debt
    budget.last_charge  # what your previous tick cost
```

**Rust**

```rust
let budget = get_budget();
budget.remaining;   // i64 -- ticks left in the bank, negative means you're in debt
budget.last_charge; // u64 -- what your previous tick cost
Budget::BANK;       // 500_000, the starting bank and the cap
Budget::REFILL;     // 600, what you get back each tick
```

It's free to call — the numbers arrive with the tick — so call it as often as you like.

The useful thing to do with it is **decide what you can afford before you spend it**:

```rust
let action = if get_budget().remaining > 100_000 {
    expensive_plan(state)      // there's bank to burn
} else {
    cheap_fallback(state)      // coast and let the refill catch up
};
```

Falling back is almost always better than overspending. An overspend isn't forgiven: it's a
debt, and you pay it off in ticks where your fleet does nothing at all.

Two things to know:

- `remaining` is what you have **going into this tick**, after last tick's charge and refill.
- `last_charge` is `0` on tick 0, which isn't charged.

## Measuring your own usage

`get_budget()` tells you the total, but not which part of your code spent it. To find that,
time the pieces yourself — with the same kind of clock the engine uses, **process CPU time**,
not a wall clock:

**Python**

```python
import time

def my_strategy(state):
    start = time.process_time()
    ...
    used_ms = (time.process_time() - start) * 1000
```

**Rust**

```rust
use cpu_time::ProcessTime;

let start = ProcessTime::now();
// ...
let used = start.elapsed();
```

Print these from your bot and look at the typical per-tick numbers and the peaks. A rough
rule: if a typical tick is well under **1ms** and your rare expensive ticks are well under
**1 second**, you're fine.

Your own laptop may be faster or slower than our tournament machine, but the budget is
relative to the engine's speed on the same machine, so how close you are to the limit should
carry over — and `get_budget().last_charge` is already in those relative units, so it carries
over directly.

## Turning it off while you develop

Enforcement gets in the way of a debugger: a breakpoint inside a tick is CPU time you're
charged for, and holding one for more than five seconds trips the hang timeout.

```
mm-cli run --no-time-limit
```

turns off **both** the budget and the hang timeout: your bot is never sat out and never timed
out, however long a tick takes. Costs are still measured and `get_budget()` still reports them
truthfully, so you can profile a slow tick and see what it *would* have cost — the bank will
go deeply negative and stay there, which is the point.

Local only. Your submitted bot is always judged with enforcement on, and a gamelog from a run
like this is marked `# time enforcement: disabled` so it can't be mistaken for a real match.

## How much can you actually afford? (Python)

Measured with a real Python bot, at 30-vs-30 fleets, on a development machine. Your refill is
600 engine ticks a tick, which on the same machine was **1.3ms early in a match and 3.3ms
late** (the engine tick gets more expensive as fleets grow). The "share" column is against the
tighter early figure, so it's the pessimistic one.

| Doing this once per tick | CPU | share of the refill |
|---|---|---|
| Iterate your fleet, reading each bot's position | 0.01 ms | 1% |
| Read **every cell of the 32×32 map** | 0.12 ms | 9% |
| Every map cell, plus a distance calculation per cell | 0.15 ms | 11% |
| **Every bot pair** (30×30), distance only | 0.32 ms | 25% |
| `navigate_to` once per bot (30 calls) | 0.05 ms | 4% |
| `path_length` once per bot (30 calls) | 0.03 ms | 2% |
| `route_waypoints` once per bot (30 calls) | 0.04 ms | 3% |
| Build a complete `FleetAction` for 30 bots | 0.10 ms | 8% |
| `line_of_sight` for every bot pair (900 calls) | 0.77 ms | 59% |
| `path_length` for every bot pair (900 calls) | 0.86 ms | 66% |
| Your own flood fill over the map, **once** | 0.36 ms | 28% |
| Your own flood fill, **once per bot** (30 runs) | 10.7 ms | **820%** |

Per unit, that works out at roughly:

- **0.15µs** per map cell in a Python loop
- **0.36µs** per bot pair in a Python loop
- **1–1.5µs** per engine query (`navigate_to`, `path_length`, `line_of_sight`, …)
- **360µs** for one hand-written flood fill over the whole map

So a single tick's refill buys you about **9,000 map cells**, **3,600 bot pairs**, or **1,300
engine queries** early in a match, and two to three times that late on.

**So: yes to both of the obvious ones.** Scanning every map cell and comparing every bot pair
are each around a tenth to a quarter of the allowance. A bot that did a full map scan, both
all-pairs distance loops *and* a `navigate_to` per bot every single tick finished a
9000-tick match without sitting out once.

What does run you out:

- **An engine query inside an all-pairs loop.** 900 `path_length` calls is two thirds of the
  early allowance on its own, and it gets worse as fleets grow.
- **Writing your own pathfinder in Python and running it per bot.** A flood fill per bot is
  eight times the allowance. Measured over a real match, that bot ran 3,603 ticks and **sat
  out 5,397** — it spent 60% of the match doing nothing at all.
- **Anything that is per-bot × per-cell.** 30 bots × 1024 cells is ~5,000 Python operations
  before you have done any work in the loop body.

A hand-written flood fill is also the thing you least need to write: `navigate_to` does the
pathfinding for you in native code, at about 1.5µs a bot.

## Tips

- **Do expensive work less often.** Path planning, target assignment and threat maps rarely
  need recomputing every tick. Cache them and refresh every N ticks or when something
  important changes.
- **Use the engine's helpers.** `navigate_to`, `path_length`, `line_of_sight` and friends run
  in native code and are much cheaper than a hand-written Python version.
- **Check the bank before a burst.** Gate your expensive path on `get_budget().remaining` and
  keep a cheap fallback. Sitting out a few hundred ticks costs you far more than one
  mediocre plan does.

## Fair play

The budget is sized so the whole final tournament fits in our infrastructure's time window.
Using your budget for real computation is fine. Deliberately stalling a match (sleeping,
spinning, or burning CPU for nothing) is not: we treat that as an attack on our
infrastructure and may disqualify you.

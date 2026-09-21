# IAMABOT — a MechMania 32 strategy bot

Team **IAMABOT**'s entry for [MechMania 32](https://mechmania.github.io/), a 24-hour AI
programming competition. Two programs each command a fleet of up to 32 bots on a walled
32×32 map. A team wins by pushing the payload all the way to the enemy's end, by wiping
out the enemy fleet, or by being ahead on the payload when the 9000-tick clock runs out.
Bots come in three classes: battle bots (hitscan blasters), healers and extractors, which
mine the tokens that pay for new bots.

During the event the bot climbed from 7th to **3rd on the live leaderboard**. At least once
it beat each of Team Name, clankerbot, DIBSFA, Gang, Potatoes, Syntax Terror, CSK and
noeyedeer.

## How it plays

Every rule the bot relies on was read from the engine's Rust source, not from the prose
rules.

- **Engine-exact fire control.** Before pulling the trigger, the bot replays each shot the
  way the engine resolves it. The ray starts from the shooter's post-move, post-turn pose
  and stops at the first enemy hull, the payload, a deposit, a wall or the map edge. Splash
  then hits every enemy within `splash + radius` of the impact point. A shot is fired only
  if it lands on a vulnerable enemy under both the predicted and the stationary enemy
  layouts. Shooters claim their victims each tick, so a volley is never wasted on a bot
  that is already immune.
- **Stand-off pressing.** Each battle bot closes on its nearest enemy and stops just inside
  blaster range (9.3 of 10). The enemy has to walk into the fire of every gun at once. Bots
  that are badly hurt back off while still shooting, and with a clear local advantage the
  line closes to 7.5 tiles to finish the fight.
- **Payload control.** The engine moves the payload only while exactly one team has a bot
  inside the capture circle. One healthy bot therefore holds a spot inside the circle,
  shielded by the payload, whenever the enemy is in it. When most of the enemy army sits on
  the payload, the army ignores decoys elsewhere and fights at the payload. A group that has
  been stuck behind a wall with no shot while the payload drifts toward our end walks back
  to it.
- **Healing.** Healers are paired with patients globally. Each healer picks a spot with a
  clear line to its patient (walls blocked a quarter of our heals before this), and heals
  any wounded ally in reach when its planned patient is not. The healer share rises to
  match the enemy's, up to 40%.
- **Economy and production.** The opening reads the enemy's first four units to recognise
  the leaders' openings and trims extractors in favour of guns and healers. New bots join
  the army before engaging instead of walking into the fight one at a time. In the last 30
  ticks before production stops, surplus extractors self-destruct and the freed slots are
  rushed as battle bots.
- **Endgame and tiebreaks.** Late in the game, if the payload sits on our half, every gun
  targets the bodies holding the circle and two bots walk in to push it back. One bot
  always hides so that a fleet wipe cannot lose the game.
- **Compute.** Hot loops run on plain floats and call the engine's pathfinding and
  line-of-sight through raw C entry points. A tick typically costs under a millisecond, and the
  compute bank stays full all game.

The full write-up, in Chinese, is in [`docs/STRATEGY_zh.md`](docs/STRATEGY_zh.md). It
covers the rules that matter, what we learned from the top teams' replays, every
mechanism, and the evidence behind each version.

## How it was built

The strategy changed only when the evidence said so:

1. **Replay forensics.** We downloaded the game logs of every loss from the tournament
   server. [`tools/arena/replay.py`](tools/arena/replay.py) reads them tick by tick: who
   holds the capture circle, how far each army stands from the payload, shots, hits,
   heals, healer duty and how tightly each side focuses its fire.
2. **Sparring partners.** Each opponent style that beat us was rebuilt from the measured
   numbers (opening order, composition, distance to the payload, bodies in the circle)
   and added to a roster of about 60 opponents.
3. **Regression arena.** [`tools/arena/arena.py`](tools/arena/arena.py) plays a candidate
   against the roster from both sides with the real engine, and lists every game that got
   worse against a baseline. A change was adopted only if it lost no game the previous
   version won.

About two dozen ideas were tested this way. More than half were rejected, including
charging the payload, walking until a line of fire opens, a three-bot payload escort,
holding fire for focus, and a larger healer share everywhere. The reasons are recorded
in the strategy document.

## Repository layout

```
strategy/        the bot (brain.py: AdvancedStrategy; main.py: entry point)
tools/arena/     regression arena, replay analysis, sparring-partner bots
docs/            strategy write-up (Chinese), game mechanics, compute budget,
                 starter-pack notes
core/, native/   MechMania starter pack: engine bindings (Python ctypes + Rust shim)
```

## Running it

You need Rust, Python 3 and the MechMania CLI (setup details are in
[`docs/STARTERPACK.md`](docs/STARTERPACK.md)):

```sh
cargo install --git https://github.com/mechmania/cli
mm-cli run            # builds the engine bindings and plays the bot against itself
```

To run the regression arena against the key opponents, both sides (about a minute on 10
cores):

```sh
python3 tools/arena/arena.py cand=live --opps key -j 10
```

`--opps full` plays the whole roster. `--baseline NAME` lists the games a candidate
newly lost or newly won compared with a baseline. See
[`tools/arena/README.md`](tools/arena/README.md).

## Team

Built by team IAMABOT at MechMania 32. See the repository's contributors for everyone
who worked on it. The `core/` and `native/` directories come from the official
[MechMania Python starter pack](https://github.com/mechmania/python-starterpack).

# Game Mechanics

This page explains how a match actually plays out, tick by tick. It's the detailed version of the overview in the [README](README.md).

## The tick

The engine runs the match in ticks. Each tick, both bots send back one action per bot in their fleet, plus fabricator orders. The engine then does, in this order:

1. **Build bots** (fabricator: rush orders, then the free natural build)
2. **Move and turn** every bot
3. **Push the payload**
4. **Resolve collisions** (walls, other bots, the payload, the deposits)
5. **Run extractors** (mining)
6. **Run healers**
7. **Run blasters** (damage, then remove dead bots)

A bot built this tick shows up in the world for steps 2–7, but it doesn't get to act on its first tick — the fleet sent its orders before the new bot existed, so there's nothing for it to do yet.

## The map

The map is a 32x32 grid of tiles. Each tile is either empty or a wall. The map is symmetric across both teams.

Each team has one deposit (for mining) sitting in its own half. A team's goal is where the enemy's payload path ends.

A bot always thinks of itself as team A, sitting bottom-left. Before the engine hands state to team B, it mirrors everything (position, angle, velocity) so team B's bot sees the same view team A does. Your strategy code never needs to check which side it's on.

## Bots

You build bots of three classes. Every bot moves and turns the same way regardless of class — what's different is its "special" action.

- **Battle**: shoots.
- **Healer**: heals allies.
- **Extractor**: mines tokens from a deposit.

A bot's stats (speed, health, range, damage, etc.) are fixed by the match config.

### Movement and turning

A bot moves in whatever direction you tell it, at a max speed. We have helper functions for pathfinding, see the `navigate_to` function.

Turning works one of three ways, your choice per tick:

- Turn at some power in [-1, 1] times the max turn speed.
- Turn toward a specific angle.
- Turn to face a specific point.

### Self-destruct

Any bot, regardless of class, can self-destruct on a given tick. It dies instantly and is removed from the fleet — there's no splash damage or other effect on anything else. It still gets to act (move, fire, heal, extract) on the tick it blows itself up, the same as a bot that dies to a blaster hit that tick.

## Combat (Battle bots)

Firing is hitscan, meaning the shot travels instantly. The ray starts at the bot's center, points along its facing, and travels up to `blaster_range`. It passes straight through your own fleet, and stops at the first thing it hits: an enemy bot, a wall, the map edge, the payload, or a deposit. If it doesn't hit anything, it just detonates in the air at max range.

Wherever the ray stops, every **enemy** bot whose hull is within the splash radius of that point takes damage, not just the one that got hit. 


A few things to know:
- Your own fleet can never be hit by your own shot, even by splash.
- A bot that gets hit becomes invulnerable for a few ticks. That's what caps it at one hit per tick, no matter how many shots land on it at once.
- The blaster has a cooldown between shots.
- Be careful not to stack your bots on top of each other, or they will become a juicy target!

## Healing (Healer bots)

A healer names one ally by ID and heals it every tick it channels. For the heal to land, the healer has to actually be facing the target: the target must be within heal range **and** within the healer's facing arc. Walls and the map edge block healing; other bots and the payload do not.

Healing can't push a bot above max health. Multiple healers can stack on one target, only up to `heal_stack_cap` healers.

## Economy

### Extractors

An extractor mines by looking at a deposit: it casts a ray from its center along its facing, and if that ray reaches a deposit before hitting a wall or the map edge, it's mining. Other bots and the payload don't block this ray.

Each deposit has a limited number of extraction slots, **shared between both teams**. If the enemy fills every slot, you get nothing from that deposit until one of their extractors stops mining. 
A bot that already holds a slot keeps it as long as it continues mining.

Every tick a bot holds a slot, its team's fabricator gains tokens.

### Fabricator

Tokens buy bots through *rush orders*. Each team's fabricator does two things:

- **Free natural build**: every `fabricator.interval` ticks, you get a new bot for free (whatever class you've set as `fabricator_next`), as long as your fleet isn't already full.
- **Rush order**: pay `rush_cost` tokens to get that same bot immediately, instead of waiting for the timer. If a rush order lands on the same tick a natural build was due, the natural build isn't lost — it just slides to the next tick, so you don't get cheated out of a bot you already paid for.

A rush order into a full fleet, or one you can't afford, is simply ignored — you don't get charged and nothing happens.

Fabricators earn nothing and build nothing during the **endgame** (see below).

## The payload

The payload sits at the center of a fixed path between the two goals. Every tick, the engine counts how many of each team's bots are within capture range of the payload. If there are no bots, or at least one bot of each team, the payload doesn't move. Otherwise, a payload controlled by a team will advance towards the opponent.

If the payload reaches a goal, the match ends immediately, the team whose goal it is loses.

## Collision

Bots can't overlap with walls, the map boundary, the payload, or a deposit. Bots do **not** collide with each other. Two bots from either team can sit on the exact same spot, which is exactly why stacking into a blaster splash is dangerous.

## Winning and the endgame

A match ends the moment one of these happens, checked in this order:

1. **The payload reaches a goal.** That team loses, immediately, whatever tick it is.
2. **During the endgame, a fleet has zero bots.** The other team wins. (If both fleets are wiped on the same tick, it goes to the tiebreakers below instead.)
3. **The match clock (`max_ticks`) runs out.** Goes to the tiebreakers.

**The endgame** is the last `endgame_ticks` of the match. Once it starts:

- Fabricators stop working completely, no more free bots, and **rush orders are refused**.
- A fleet that hits zero bots during this window loses on the spot.

**Tiebreakers**, checked in this order:

1. Which side of center the payload is on (whoever pushed it further toward the enemy goal).
2. Total health across your surviving bots.
3. Tokens banked.
4. If all three are exactly equal: a draw.

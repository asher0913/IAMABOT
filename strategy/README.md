# strategy/

`main.py` returns `AdvancedStrategy` from `brain.py` for both sides. The engine mirrors the
map for team B, so the controller only ever reasons about the bottom-left start.

`brain.py` is organised by tick phase:

1. **Snapshot**: fleets are copied into plain floats once per tick. Engine helpers
   (pathfinding, line of sight, free discs) are called through raw C entry points, and
   checked at startup against the official wrappers.
2. **Situation**: the army centroid, the enemy front, who holds the capture circle, the
   payload's recent movement, and raids on our deposit.
3. **Production**: the opening, the extractor and healer targets (adapted to the enemy's
   opening and to its healer share), rushes, and the endgame trade of extractors for battle
   bots.
4. **Movement**: stand-off pressing, the circle sentry, fighting at the payload when the
   enemy commits to it, rejoining the army, retreating the badly hurt, deposit guards,
   extractor slots, healer pairing and spots with a clear line, and bot separation.
5. **Aim and fire**: global aim assignment, then an exact replay of each ray before the
   trigger is pulled.

Every behaviour added after v8 has an `IAMABOT_*` environment switch and a default. The
tournament sets none, so the defaults are what plays. The switches let
`tools/arena/arena.py` A/B-test a change from both sides without editing the code, for
example `cand=live:IAMABOT_SENTRY=0`.

With `IAMABOT_STATS=1` (the arena sets it), the bot prints a `[stats]` line every 250
ticks: shots fired, shots blocked by the payload or a wall, heals landed and why the rest
missed, the lowest compute bank, and how often fallbacks were used. If a tick raises, the
error is logged (rate limited) and a simple fallback controller acts for that tick, so the
fleet never stands idle.

The full design rationale and the evidence behind each version, in Chinese, are in
[`../docs/STRATEGY_zh.md`](../docs/STRATEGY_zh.md).

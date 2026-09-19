"""IAMABOT tournament controller.

Everything here works in the engine's mirrored frame (we are always team A, bottom-left),
so the same code plays both sides.

The design follows the engine source rather than the prose rules:

* A blaster shot is resolved with the shooter's *post-move, post-turn* pose against the
  enemies' post-move positions; the ray stops at the first enemy hull, the payload, a
  deposit, a wall or the map edge, and splashes every enemy whose centre is within
  ``splash + radius`` of that point.  We replay exactly that before pulling a trigger, so
  no shot is spent on an invulnerable bot, on the payload or on thin air.
* A hit bot is immune for ``base_invulnerability_ticks``; shooters claim victims per tick so
  one volley is spread over several bodies instead of five shots landing as one.
* The payload is solid and sits in a four-tile corridor, so it eats every shot fired along
  the corridor axis.  Firing positions are chosen against the enemy's actual front with a
  clear line past the payload, not in a ring behind it.
* The payload moves at a fixed speed whenever exactly one team has a bot inside the capture
  radius, independent of how many.  With no enemy fighter around, the army takes the circle
  and pushes.
* When enemy fighters exist, every battle bot presses its nearest one and stands to shoot
  just inside blaster range (9.3 of 10): all guns engage at once, converge on the enemy's
  nearest bodies, and the enemy has to walk into our fire.  Head to head this beat every
  firing-position planner we tried and every shorter distance, including our own v5/v6.
* From the endgame on, if the payload sits on our half, every gun goes for the bodies
  holding the circle at close range and two bots walk in to push it back: sitting still
  there is a certain tiebreak loss.
* Badly hurt battle bots (4 hp or less, one hit from dying) back out of reach while still
  shooting (facing is independent of movement) and return once healed to 8; with a clear
  local edge (1.5x) the stand-off distance closes from 9.3 to 7.5 to finish the fight.
* Several adaptive responses (all-in home raid answer, counter-economy raid, zone-aware
  distance, payload sidestep, strafing, payload leash) are implemented but off by default:
  in the evaluation matrices each of them cost more games than it won.

All hot loops use plain floats; engine helpers are called through the raw C entry points
to avoid building ``Vec2`` objects in the inner loops.
"""

from __future__ import annotations

import math
import os

from . import *
from .v10 import AdvancedStrategyV10 as _VerifiedStrategy

try:  # raw entry points: same functions as core.channel, minus the Vec2 wrapping
    import ctypes as _ctypes

    from core import channel as _channel_mod
    from core._generated import bindings as _raw

    _RAW_OK = True
except Exception:  # pragma: no cover - defensive, the starterpack always has these
    _RAW_OK = False


DEBUG = bool(os.environ.get("IAMABOT_DEBUG"))
# Per-match counters for the regression harness (tools/arena); off in tournament play.
STATS = bool(os.environ.get("IAMABOT_STATS"))

BATTLE = 0
HEALER = 1
EXTRACTOR = 2

RAD = math.pi / 180.0


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return type(default)(raw)


def _ang(dx: float, dy: float) -> float:
    return math.atan2(dy, dx) / RAD % 360.0


def _adiff(a: float, b: float) -> float:
    """Signed shortest rotation from b to a, degrees."""
    return (a - b + 180.0) % 360.0 - 180.0


def _ray_circle(ox, oy, dx, dy, cx, cy, r):
    """Engine `ray_circle`: nearest t >= 0 where the unit ray meets the circle, or None."""
    mx = cx - ox
    my = cy - oy
    b = mx * dx + my * dy
    c = mx * mx + my * my - r * r
    if c > 0.0 and b < 0.0:
        return None
    disc = b * b - c
    if disc < 0.0:
        return None
    t = b - math.sqrt(disc)
    return t if t > 0.0 else 0.0


def _ray_boundary(ox, oy, dx, dy, size):
    t = 1e9
    if abs(dx) > 0.001:
        t = min(t, ((size if dx > 0 else 0.0) - ox) / dx)
    if abs(dy) > 0.001:
        t = min(t, ((size if dy > 0 else 0.0) - oy) / dy)
    return max(t, 0.0)


def _seg_dist2(px, py, ax, ay, bx, by) -> float:
    """Squared distance from point p to segment a-b."""
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    L = vx * vx + vy * vy
    t = 0.0 if L <= 1e-12 else max(0.0, min(1.0, (wx * vx + wy * vy) / L))
    dx = wx - vx * t
    dy = wy - vy * t
    return dx * dx + dy * dy


class Unit:
    __slots__ = ("id", "cls", "x", "y", "vx", "vy", "ang", "hp", "inv", "nft", "px", "py")


def _snapshot(fleet) -> list:
    out = []
    for b in fleet:
        sp = b.special
        tag = sp.tag
        u = Unit()
        u.id = b.id
        u.cls = tag
        p = b.pos
        u.x = p.x
        u.y = p.y
        v = b.vel
        u.vx = v.x
        u.vy = v.y
        u.ang = b.angle
        u.hp = b.health
        u.inv = b.invulnerable_until_tick
        u.nft = sp.payload.battle.next_fire_tick if tag == BATTLE else 0
        u.px = u.x + u.vx
        u.py = u.y + u.vy
        out.append(u)
    return out


class AdvancedStrategy:
    """Fire-control, positioning, healing and economy controller."""

    # Production plan.  The opening is balanced: three extractors start the economy at
    # once (every top team opens with seven to ten, and an all-fighter opening falls behind
    # them by mid game), ten battle bots and four healers win the first fight.  A third of
    # the fighting force stays healers: sustain beat raw guns in every head-to-head.
    OPENING = _env("IAMABOT_OPENING", "EBBEHBEBBHBBHBBHB")
    EXTRACTOR_TARGET = _env("IAMABOT_EXTRACTORS", 6)
    HEALER_RATIO = _env("IAMABOT_HEALER_RATIO", 0.33)
    EXTRACTOR_CUTOFF = 4300  # an extractor built later cannot pay for itself

    # Engagement distances (to the nearest enemy fighter) per stance.
    D_ATTACK = _env("IAMABOT_D_ATTACK", 6.3)
    D_HOLD = _env("IAMABOT_D_HOLD", 7.4)
    ATTACK_RATIO = _env("IAMABOT_ATTACK_RATIO", 1.25)
    RETREAT_RATIO = _env("IAMABOT_RETREAT_RATIO", 0.72)

    MOVE_MODE = _env("IAMABOT_MOVE", "press")
    D_PRESS = _env("IAMABOT_D_PRESS", 9.3)
    D_NEAR = _env("IAMABOT_D_NEAR", 6.0)
    ZONE_MARGIN = _env("IAMABOT_ZONE_MARGIN", -3.0)
    LATE_TICKS = _env("IAMABOT_LATE", 3000)
    SIDESTEP = _env("IAMABOT_SIDESTEP", 0)
    RETREAT_HP = _env("IAMABOT_RETREAT_HP", 4.0)
    RETURN_HP = _env("IAMABOT_RETURN_HP", 8.0)
    STRAFE = _env("IAMABOT_STRAFE", 0)
    DYN_RANGE = _env("IAMABOT_DYN_RANGE", 1)
    DYN_EDGE = _env("IAMABOT_DYN_EDGE", 1.5)
    D_CLOSE = _env("IAMABOT_D_CLOSE", 7.5)
    LEASH = _env("IAMABOT_LEASH", 0.0)
    GUARDS = _env("IAMABOT_GUARDS", 1)
    STEAL = _env("IAMABOT_STEAL", 0)
    RACE_LEASH = _env("IAMABOT_RACE_LEASH", 10.0)
    RACE_ABORT = _env("IAMABOT_RACE_ABORT", 0.8)
    PUSH_MODE = _env("IAMABOT_PUSH", 1)
    PUSH_NEAR = _env("IAMABOT_PUSH_NEAR", 12.0)
    PUSH_LEASH = _env("IAMABOT_PUSH_LEASH", 11.0)
    PUSH_HOME_R = _env("IAMABOT_PUSH_HOME_R", 11.0)
    PUSH_ENTER = _env("IAMABOT_PUSH_ENTER", 120)
    PUSH_EXIT = _env("IAMABOT_PUSH_EXIT", 60)
    SIDESTEP_STALL = _env("IAMABOT_SIDESTEP_STALL", 300)
    PRESS_LOS = _env("IAMABOT_PRESS_LOS", 0)
    STALL_TICKS = _env("IAMABOT_STALL", 250)
    RAID = _env("IAMABOT_RAID", 0)
    RAID_MIN_MINERS = _env("IAMABOT_RAID_MIN", 4)
    RAID_EDGE = _env("IAMABOT_RAID_EDGE", 1.4)
    HOME_R = _env("IAMABOT_HOME_R", 12.0)
    HOME_EDGE = _env("IAMABOT_HOME_EDGE", 1.4)
    HOME_MODE = _env("IAMABOT_HOME", "off")

    CHEAP_BUDGET = 60_000

    def __init__(self) -> None:
        self.init_done = False
        self.aim: dict[int, int] = {}  # battle id -> enemy id
        self.heal_target: dict[int, int] = {}
        self.mine_slot: dict[int, int] = {}
        self.slot_key = None
        self.slot_tick = -999
        self.slots: list = []
        self.slot_owner: dict[int, int] = {}
        self.stance = "hold"
        self.stance_since = 0
        self.raid: list = []
        self.n_guards = 0
        self.capture_moved = 0
        self.last_capture = 0.0
        self.raiders: set = set()
        self.home = None
        self.home_raid: list = []
        self.home_ids: set = set()
        self.home_phase = "gather"
        self.home_since = 0
        self.retreating: set = set()
        self.push = False
        self.push_count = 0
        self.stealing = False
        self.debug_next = 0
        self.why: dict = {}
        self.exc_count = 0
        self.stats = {
            "ticks": 0, "shots": 0, "ready_idle": 0, "blocked_payload": 0, "blocked_wall": 0,
            "heal_try": 0, "heal_ok": 0, "retreat_enter": 0, "flank_enter": 0,
            "race_ticks": 0, "defend_ticks": 0, "steal_ticks": 0,
            "min_bank": 10**9, "fallbacks": 0, "exceptions": 0,
        }

    # ================================================================== setup

    def _setup(self, state: GameState) -> None:
        conf = get_config()
        self.conf = conf
        b = conf.bot
        self.R = b.radius
        self.SPEED = b.speed
        self.TURN = b.turn_speed
        self.RANGE = b.blaster_range
        self.DMG = b.blaster_damage
        self.MAXHP = b.health
        self.SPLASH = b.base_blaster_splash_radius + b.radius
        self.HEAL_R = b.base_heal_range
        self.HEAL_HALF = b.base_heal_arc_deg / 2.0
        self.STACK = int(b.heal_stack_cap + 1e-6)
        self.EXTRACT_R = b.base_extract_range
        self.P_R = conf.payload.radius
        self.CAP_R = conf.payload.capture_radius
        self.DEP_R = conf.deposit.radius
        self.SIZE = float(MAP_SIZE)
        self.END_T = conf.max_ticks - conf.endgame_ticks
        self.MAX_T = conf.max_ticks
        # Keep bots this far from a solid hull (payload/deposit) so a shot that hits the
        # hull cannot splash them: hull distance must exceed the splash reach.
        self.HULL_SAFE = b.base_blaster_splash_radius + b.radius + 0.08
        self.SPACING = 2.0 * self.R + b.base_blaster_splash_radius + 0.22  # ~1.02
        # A shot line passing this close to the payload centre is eaten by the payload.
        self.P_BLOCK2 = (self.P_R + 0.05) ** 2

        self.raw = False
        if _RAW_OK:
            try:
                self._h = _channel_mod._channel._handle
                self._los_fn = _raw.mm_line_of_sight
                self._nav_fn = _raw.mm_navigate_to
                self._navbuf = (_ctypes.c_float * 2)()
                self._free_fn = _raw.mm_disc_free
                # Sanity-check the raw path against the public wrapper once.
                probe = self._los_fn(self._h, 1.0, 1.0, 2.0, 2.0)
                self.raw = probe == line_of_sight(Vec2(1.0, 1.0), Vec2(2.0, 2.0))
            except Exception:
                self.raw = False

        d = state.deposit_me.pos
        self.dep = (d.x, d.y)
        d2 = state.deposit_other.pos
        self.dep_other = (d2.x, d2.y)
        # Spawn corner in our frame (engine spawns team A at its own goal corner).
        self.spawn = (self.R + 0.001, self.SIZE - self.R - 0.001)
        self.enemy_spawn = (self.SIZE - self.R - 0.001, self.R + 0.001)

        # Static grids of standable points with a small clearance margin.
        fine = []
        step = 0.5
        n = int(self.SIZE / step)
        for i in range(1, n):
            x = i * step
            for j in range(1, n):
                y = j * step
                if self._disc_free(x, y, self.R + 0.06):
                    fine.append((x, y))
        self.grid = fine
        self.coarse = [(x, y) for (x, y) in fine if (x * 2) % 2 == 1 and (y * 2) % 2 == 1]
        self.mine_slots = self._make_mine_slots()
        self.steal_slots = self._slots_around(self.dep_other)
        self.init_done = True

    # ============================================================ engine calls

    def _los(self, ax, ay, bx, by) -> bool:
        if self.raw:
            return self._los_fn(self._h, ax, ay, bx, by)
        return line_of_sight(Vec2(ax, ay), Vec2(bx, by))

    def _disc_free(self, x, y, r) -> bool:
        if self.raw:
            return self._free_fn(self._h, x, y, r)
        return disc_free(Vec2(x, y), r)

    def _nav(self, fx, fy, tx, ty):
        if self.raw:
            buf = self._navbuf
            self._nav_fn(self._h, fx, fy, tx, ty, buf)
            dx, dy = buf[0], buf[1]
        else:
            v = navigate_to(Vec2(fx, fy), Vec2(tx, ty))
            dx, dy = v.x, v.y
        n = math.hypot(dx, dy)
        if n > 1.0:
            dx /= n
            dy /= n
        return dx, dy

    def _payload(self, capture: float):
        v = payload_pos(max(-1.0, min(1.0, capture)))
        return v.x, v.y

    def _payload_blocks(self, ax, ay, bx, by) -> bool:
        px, py = self.P
        # A target hugging the payload's far side is still behind it; one on our side is
        # not.  The segment test covers both.
        return _seg_dist2(px, py, ax, ay, bx, by) < self.P_BLOCK2

    # ================================================================ geometry

    def _slots_around(self, dep) -> list:
        """Spread mining spots with a clear ray to `dep` (used for the enemy deposit)."""
        dx0, dy0 = dep
        reach = self.EXTRACT_R + self.DEP_R - 0.35
        min_r = self.DEP_R + self.HULL_SAFE
        cand = []
        for (x, y) in self.grid:
            r = math.hypot(x - dx0, y - dy0)
            if r < min_r or r > reach or not self._los(x, y, dx0, dy0):
                continue
            cand.append((-abs(r - 2.5), x, y))
        cand.sort(reverse=True)
        chosen: list = []
        for _, x, y in cand:
            if all((x - a) ** 2 + (y - b) ** 2 >= self.SPACING ** 2 for a, b in chosen):
                chosen.append((x, y))
            if len(chosen) >= 10:
                break
        return chosen

    def _make_mine_slots(self) -> list:
        dx0, dy0 = self.dep
        reach = self.EXTRACT_R + self.DEP_R - 0.35  # centre distance the ray still reaches
        min_r = self.DEP_R + self.HULL_SAFE
        sx, sy = self.spawn
        cand = []
        for (x, y) in self.grid:
            r = math.hypot(x - dx0, y - dy0)
            if r < min_r or r > reach:
                continue
            if not self._los(x, y, dx0, dy0):
                continue
            score = -abs(r - 2.3) * 0.8 - math.hypot(x - sx, y - sy) * 0.08
            # Tucked away from the map centre (where the enemy comes from).
            score += (y - 16.0) * 0.05
            cand.append((score, x, y))
        cand.sort(reverse=True)
        chosen: list = []
        for _, x, y in cand:
            if all((x - a) ** 2 + (y - b) ** 2 >= self.SPACING ** 2 for a, b in chosen):
                chosen.append((x, y))
            if len(chosen) >= 12:
                break
        if not chosen:
            chosen.append((dx0 - 2.0, dy0))
        return chosen

    # ============================================================== main entry

    def __call__(self, state: GameState) -> FleetAction:
        try:
            if not self.init_done:
                self._setup(state)
            act = self._tick(state)
            if STATS:
                self._stat_tick(state)
            return act
        except Exception as exc:  # never let a bug take the whole fleet out of the match
            self.exc_count += 1
            self.stats["exceptions"] += 1
            if self.exc_count <= 3 or self.exc_count % 500 == 0:
                import sys
                import traceback

                print(
                    f"[iamabot] {type(exc).__name__} at tick {getattr(state, 'tick', '?')} "
                    f"(#{self.exc_count}): {exc}",
                    file=sys.stderr,
                )
                if DEBUG:
                    traceback.print_exc()
            try:
                self.stats["fallbacks"] += 1
                return self._fallback(state)
            except Exception:
                return FleetAction.new()

    def _stat_tick(self, state: GameState) -> None:
        st = self.stats
        st["ticks"] += 1
        st["min_bank"] = min(st["min_bank"], get_budget().remaining)
        if self.home == "race":
            st["race_ticks"] += 1
        elif self.home == "defend":
            st["defend_ticks"] += 1
        if self.stealing:
            st["steal_ticks"] += 1
        if state.tick % 250 == 0 or state.tick >= self.MAX_T - 1:
            import json as _json

            print("[stats] " + _json.dumps(st), flush=True)

    # ================================================================ the tick

    def _tick(self, state: GameState) -> FleetAction:
        T = state.tick
        self.T = T
        cheap = get_budget().remaining < self.CHEAP_BUDGET
        self.cheap = cheap
        act = FleetAction.new()

        me = _snapshot(state.fleet_me)
        op = _snapshot(state.fleet_other)
        self.me = me
        self.op = op
        self.op_by_id = {e.id: e for e in op}
        if abs(state.capture - self.last_capture) > 1e-6:
            self.capture_moved = T
            self.last_capture = state.capture
        self.capture = state.capture
        px, py = self._payload(state.capture)
        self.P = (px, py)
        self.endgame = T >= self.END_T

        battles = [u for u in me if u.cls == BATTLE]
        healers = [u for u in me if u.cls == HEALER]
        extractors = [u for u in me if u.cls == EXTRACTOR]
        op_fighters = [e for e in op if e.cls != EXTRACTOR]
        self.op_fighters = op_fighters

        # ---- situation -------------------------------------------------------
        cap2 = self.CAP_R ** 2
        self.op_in_zone = [e for e in op if (e.px - px) ** 2 + (e.py - py) ** 2 <= cap2]
        self.me_in_zone = [u for u in me if (u.x - px) ** 2 + (u.y - py) ** 2 <= cap2]
        self.u = self._enemy_dir(op_fighters)
        self._assess(battles, healers, op_fighters)

        self._production(state, act, me)

        # ---- movement ----------------------------------------------------------
        moves: dict[int, tuple] = {}
        self._home_state()
        ex, ey = self.dep_other
        self.stealing = bool(
            self.STEAL
            and self.home is not None
            and not any(
                (e.x - ex) ** 2 + (e.y - ey) ** 2 < 9.0 ** 2 for e in self.op if e.cls == BATTLE
            )
        )
        if self.HOME_MODE == "steal":
            # Economy-only answer: the miners take the enemy's empty deposit, the army
            # plays exactly as without the raid.
            self.home = None
        if self.home:
            # All-in raid on our deposit: the whole army answers it (or races the payload);
            # no trickle of guards, and the miners run.
            guards = {}
            self.raid = self.home_raid
        elif self.GUARDS:
            guards = self._pick_guards(battles, op)
        else:
            guards = {}
            self._pick_guards([], op)  # still flags the raid so the miners run
        self.n_guards = len(guards)
        raid = self._pick_raiders(battles, guards)
        moves.update(raid)
        front = [b for b in battles if b.id not in guards and b.id not in raid]
        self._battle_moves(front, moves)
        for b in battles:
            if b.id in guards:
                gx, gy = guards[b.id]
                moves[b.id] = self._nav(b.x, b.y, gx, gy)
        mine_flags = self._extractor_moves(extractors, moves)
        heal_plans = self._healer_moves(healers, me, moves)
        self._separate(me, moves)
        heal_plans = self._heal_triggers(healers, heal_plans, moves)

        # ---- aiming and fire -------------------------------------------------
        self._assign_aims(battles, op)
        fire = self._fire_control(battles, op, moves)

        # ---- write actions -----------------------------------------------------
        for u in me:
            ba = act.bots[u.id]
            mx, my = moves.get(u.id, (0.0, 0.0))
            ba.move_action = MoveAction(Vec2(mx, my))
            ox = u.x + mx * self.SPEED
            oy = u.y + my * self.SPEED
            if u.cls == BATTLE:
                ang, go = fire.get(u.id, (None, False))
                if ang is None:
                    ang = self._battle_facing(u, ox, oy)
                ba.turn_action = TurnAction.TargetRotation(deg=ang % 360.0)
                ba.special_action = SpecialAction.Battle(fire=go)
            elif u.cls == HEALER:
                target, ang, go = heal_plans.get(u.id, (0, None, False))
                if ang is None:
                    ang = _ang(px - ox, py - oy)
                ba.turn_action = TurnAction.TargetRotation(deg=ang % 360.0)
                ba.special_action = SpecialAction.Healer(fire=go, target=target)
            else:
                ang, mine = mine_flags.get(u.id, (None, False))
                if ang is None:
                    ang = _ang(self.dep[0] - ox, self.dep[1] - oy)
                ba.turn_action = TurnAction.TargetRotation(deg=ang % 360.0)
                ba.special_action = SpecialAction.Extractor(mine=mine)

        if DEBUG and T >= self.debug_next:
            self.debug_next = T + 250
            b = get_budget()
            print(
                f"[dbg] t={T} stance={self.stance} cap={state.capture:+.3f} "
                f"near={self.my_near:.1f}v{self.op_near:.1f} all={self.my_all:.1f}v{self.op_all:.1f} "
                f"B/H/E={len(battles)}/{len(healers)}/{len(extractors)} op={len(op)} "
                f"zone={len(self.me_in_zone)}v{len(self.op_in_zone)} guards={self.n_guards} raid={len(self.raiders)} home={self.home}/{self.home_phase} steal={self.stealing} "
                f"AC=({self.AC[0]:.0f},{self.AC[1]:.0f}) "
                f"tok={state.fabricator_me.tokens:.0f} bank={b.remaining} last={b.last_charge} "
                f"why={self.why}",
                flush=True,
            )
            self.why = {}
        return act

    # ============================================================== assessment

    def _power(self, u) -> float:
        return (u.hp / self.MAXHP) * (1.0 if u.cls == BATTLE else 0.5)

    def _assess(self, battles, healers, op_fighters) -> None:
        """Army centroid, the enemy front it faces, and a press/hold/retreat stance."""
        T = self.T
        px, py = self.P
        fighters = battles + healers
        # Our army: battle bots near the objective, else all of them.
        core = [b for b in battles if (b.x - px) ** 2 + (b.y - py) ** 2 <= 14.0 ** 2]
        pool = core or battles
        if pool:
            ax = sum(b.x for b in pool) / len(pool)
            ay = sum(b.y for b in pool) / len(pool)
        else:
            ax, ay = px, py
        self.AC = (ax, ay)

        # Enemy front: fighters threatening the objective or our army.
        rel = []
        for e in op_fighters:
            dp = (e.px - px) ** 2 + (e.py - py) ** 2
            da = (e.px - ax) ** 2 + (e.py - ay) ** 2
            if dp <= 14.0 ** 2 or da <= 12.0 ** 2:
                rel.append(e)
        # Enemy extractors sitting in the capture circle also have to die.
        for e in self.op_in_zone:
            if e.cls == EXTRACTOR:
                rel.append(e)
        self.rel = rel

        near_r2 = 13.0 ** 2
        fx, fy = (px, py)
        if rel:
            fx = sum(e.px for e in rel) / len(rel)
            fy = sum(e.py for e in rel) / len(rel)
        self.front_c = (fx, fy)
        my_near = sum(
            self._power(u)
            for u in fighters
            if min((u.x - fx) ** 2 + (u.y - fy) ** 2, (u.x - px) ** 2 + (u.y - py) ** 2) <= near_r2
        )
        op_near = sum(self._power(e) for e in rel if e.cls != EXTRACTOR)
        my_all = sum(self._power(u) for u in fighters)
        op_all = sum(self._power(e) for e in op_fighters)
        self.my_near, self.op_near, self.my_all, self.op_all = my_near, op_near, my_all, op_all

        # Reinforcements already on their way count, discounted.
        mine = my_near + 0.5 * (my_all - my_near)
        stance = self.stance
        if op_near < 0.5:
            want = "hold"
        elif mine >= self.ATTACK_RATIO * op_near + 0.5:
            want = "attack"
        elif mine < self.RETREAT_RATIO * op_near:
            want = "retreat"
        else:
            want = "hold"
        if self.endgame and self.capture < 0.0 and want == "retreat":
            want = "hold"  # the clock is on the enemy's side now; contest
        if want != stance and (T - self.stance_since >= 40 or want == "retreat"):
            self.stance = want
            self.stance_since = T

    def _enemy_dir(self, op_fighters):
        px, py = self.P
        sx = sy = w = 0.0
        for e in op_fighters:
            d = math.hypot(e.x - px, e.y - py)
            if d < 15.0:
                k = 1.0 / (1.0 + d)
                sx += (e.x - px) * k
                sy += (e.y - py) * k
                w += k
        if w > 0.0:
            n = math.hypot(sx, sy)
            if n > 1e-3:
                return sx / n, sy / n
        # No visible enemy: they come along the payload path from their side.
        ax, ay = self._payload(self.capture + 0.04)
        dx, dy = ax - px, ay - py
        n = math.hypot(dx, dy)
        if n < 1e-3:
            return 0.7071, -0.7071
        return dx / n, dy / n

    # ============================================================== production

    def _production(self, state: GameState, act: FleetAction, me: list) -> None:
        counts = [0, 0, 0]
        for u in me:
            counts[u.cls] += 1
        total = len(me)
        T = state.tick
        act.fabricator_next = self._next_class(counts, T)
        act.rush_order = (
            T < self.END_T
            and total < BOTS_MAX
            and state.fabricator_me.tokens >= self.conf.fabricator.rush_cost
        )

    def _next_class(self, counts, T) -> int:
        nb, nh, ne = counts
        if T < 60:
            want = {"B": 0, "H": 0, "E": 0}
            for ch in self.OPENING:
                want[ch] += 1
                if ch == "B" and nb < want["B"]:
                    return BATTLE
                if ch == "H" and nh < want["H"]:
                    return HEALER
                if ch == "E" and ne < want["E"]:
                    return EXTRACTOR
        dep_safe = not any(
            (e.x - self.dep[0]) ** 2 + (e.y - self.dep[1]) ** 2 < 10.0 ** 2
            for e in self.op
            if e.cls == BATTLE
        )
        # Economy only while the army is holding its own: a lost fight needs guns now.
        army_ok = self.my_all >= 0.9 * self.op_all
        if (
            ne < self.EXTRACTOR_TARGET
            and T < self.EXTRACTOR_CUTOFF
            and dep_safe
            and army_ok
            and nb >= 6
        ):
            return EXTRACTOR
        if nb >= 5 and nh < int(self.HEALER_RATIO * (nb + nh) + 0.5):
            return HEALER
        return BATTLE

    # ================================================================ guards

    def _home_state(self) -> None:
        """Detect an all-in raid on our deposit (Gang-style: most of the enemy army plus
        its extractors take our deposit) and choose the answer.

        defend: we are clearly stronger.  The army first gathers at a staging point just
        outside the raiders' reach, then strikes together, so it never files through a
        corridor into an entrenched group one bot at a time (the way v6.1 lost to Gang).
        race:   we are not.  The raiders are ignored, the miners run, and the army takes
        the payload; the enemy must come back and fight on ground we chose, or lose."""
        dx0, dy0 = self.dep
        raid = [
            e
            for e in self.op_fighters
            if (e.x - dx0) ** 2 + (e.y - dy0) ** 2 < self.HOME_R ** 2
        ]
        R = sum(self._power(e) for e in raid)
        self.home_raid = raid
        self.home_ids = {e.id for e in raid}
        M = self.my_all
        if not raid or R < 1.0:
            self.home = None
            return
        if self.HOME_MODE == "off":
            self.home = None
            return
        if self.home is None:
            if R >= 2.5 and R >= 0.5 * self.op_all:
                self.home = "defend" if M >= self.HOME_EDGE * R + 1.0 else "race"
                if self.HOME_MODE in ("defend", "race", "steal"):
                    self.home = "race" if self.HOME_MODE == "steal" else self.HOME_MODE
                self.home_phase = "gather"
                self.home_since = self.T
            return
        if self.HOME_MODE in ("defend", "race", "steal"):
            return
        if self.home == "race" and M >= (self.HOME_EDGE + 0.2) * R + 1.0:
            self.home = "defend"
            self.home_phase = "gather"
            self.home_since = self.T
        elif self.home == "defend" and M < 1.0 * R:
            self.home = "race"

    def _home_gather(self, front, moves) -> bool:
        """Gather phase of the home defence.  Returns True while still gathering."""
        if self.home_phase != "gather":
            return False
        raid = self.home_raid
        ax, ay = self.AC
        cache = self.__dict__.get("_stage_cache")
        if cache and self.T - cache[0] < 30:
            sx, sy = cache[1]
        else:
            sx, sy = self._staging(ax, ay, raid, self.RANGE + 1.5)
            self._stage_cache = (self.T, (sx, sy))
        near = sum(1 for b in front if (b.x - sx) ** 2 + (b.y - sy) ** 2 < 4.0 ** 2)
        close = any((e.x - sx) ** 2 + (e.y - sy) ** 2 < (self.RANGE + 0.5) ** 2 for e in raid)
        if near >= 0.75 * len(front) or close or self.T - self.home_since > 350:
            self.home_phase = "strike"
            return False
        for b in front:
            moves[b.id] = self._nav(b.x, b.y, sx, sy)
        return True

    def _staging(self, fx, fy, targets, stop):
        """Walk the navigation route from (fx, fy) toward the targets' centroid and stop
        at the first point `stop` away from the nearest of them."""
        tx = sum(e.x for e in targets) / len(targets)
        ty = sum(e.y for e in targets) / len(targets)
        x, y = fx, fy
        stop2 = stop * stop
        for _ in range(160):
            if min((e.x - x) ** 2 + (e.y - y) ** 2 for e in targets) <= stop2:
                break
            dx, dy = self._nav(x, y, tx, ty)
            n = math.hypot(dx, dy)
            if n < 1e-6:
                break
            x += dx / n * 0.5
            y += dy / n * 0.5
        return x, y

    def _pick_guards(self, battles, op) -> dict:
        """Battle bots peeled off to protect the extractors.  Only a force that can win
        the local fight is sent; against a raid that big the miners run instead, so the
        army is never fed to the deposit a few bots at a time."""
        dx0, dy0 = self.dep
        self.raid = []
        raid = [
            e
            for e in op
            if e.cls != EXTRACTOR and (e.x - dx0) ** 2 + (e.y - dy0) ** 2 < 11.0 ** 2
        ]
        if not raid or self.endgame:
            return {}
        self.raid = raid
        need = 1.5 * sum(self._power(e) for e in raid) + 0.5
        cap = len(battles) // 2
        order = sorted(battles, key=lambda b: (b.x - dx0) ** 2 + (b.y - dy0) ** 2)
        chosen = []
        power = 0.0
        for b in order[:cap]:
            if power >= need:
                break
            chosen.append(b)
            power += self._power(b)
        # Bots already standing at the deposit fight regardless; otherwise only commit
        # a detachment that can actually win.
        local = [b for b in chosen if (b.x - dx0) ** 2 + (b.y - dy0) ** 2 < 9.0 ** 2]
        if power < 0.8 * need:
            chosen = local
        if not chosen:
            return {}
        tx = sum(e.x for e in raid) / len(raid)
        ty = sum(e.y for e in raid) / len(raid)
        vx, vy = tx - dx0, ty - dy0
        d = math.hypot(vx, vy) or 1.0
        vx, vy = vx / d, vy / d
        out = {}
        n = len(chosen)
        for i, b in enumerate(chosen):
            off = (i - (n - 1) / 2.0) * self.SPACING
            gx = dx0 + vx * min(3.0, d * 0.5) - vy * off
            gy = dy0 + vy * min(3.0, d * 0.5) + vx * off
            out[b.id] = (gx, gy)
        return out

    def _pick_raiders(self, battles, guards) -> dict:
        """Counter-economy raid.  Economy-first opponents (the top of the leaderboard opens
        with 7-10 extractors) park their miners on their own deposit while the army is
        elsewhere.  When those miners sit unguarded, a small squad walks over and kills
        them; the enemy's reinforcements dry up and the game ends instead of stalling.
        Returns {bot id: move} for the squad."""
        T = self.T
        ex, ey = self.dep_other
        miners = [
            e for e in self.op if e.cls == EXTRACTOR and (e.x - ex) ** 2 + (e.y - ey) ** 2 < 7.0 ** 2
        ]
        active = bool(self.raiders)
        pool = [b for b in battles if b.id not in guards]
        if (
            not self.RAID
            or T < 600
            or len(miners) < (2 if active else self.RAID_MIN_MINERS)
            or len(pool) < 6
            or self.my_all < self.RAID_EDGE * self.op_all
        ):
            self.raiders = set()
            return {}
        defenders = [
            e for e in self.op_fighters if (e.x - ex) ** 2 + (e.y - ey) ** 2 < 11.0 ** 2
        ]
        need = max(3, int(1.5 * sum(self._power(e) for e in defenders) + 2.5))
        cap = max(3, len(pool) // 3)
        if need > cap:
            self.raiders = set()  # too well defended to be worth a detachment
            return {}
        current = [b for b in pool if b.id in self.raiders]
        others = sorted(
            (b for b in pool if b.id not in self.raiders),
            key=lambda b: (b.x - ex) ** 2 + (b.y - ey) ** 2,
        )
        squad = (current + others[: max(0, need - len(current))])[:cap]
        self.raiders = {b.id for b in squad}
        out = {}
        stand2 = 5.0 ** 2
        for b in squad:
            m = min(miners, key=lambda o: (o.x - b.x) ** 2 + (o.y - b.y) ** 2)
            d2 = (m.x - b.x) ** 2 + (m.y - b.y) ** 2
            if d2 > stand2 or not self._los(b.x, b.y, m.x, m.y):
                out[b.id] = self._nav(b.x, b.y, m.x, m.y)
            else:
                out[b.id] = (0.0, 0.0)
        return out

    def _flee_point(self, x, y, threats):
        """A nearby standable point far from the given threats."""
        best = None
        bs = -1e9
        for (gx, gy) in self.coarse:
            d2 = (gx - x) ** 2 + (gy - y) ** 2
            if d2 > 12.0 ** 2:
                continue
            dt = min((gx - e.x) ** 2 + (gy - e.y) ** 2 for e in threats)
            s = min(dt, 13.0 ** 2) - 0.3 * d2
            if s > bs:
                bs, best = s, (gx, gy)
        return best or self.spawn

    # ============================================================= positioning

    def _battle_moves(self, front, moves) -> None:
        """Press: every battle bot closes on its nearest enemy fighter until it is inside
        `D_PRESS` (just inside blaster range), then stands and shoots.  All guns engage at once and converge on the enemy's nearest bodies, which
        beat every formation we tried head to head.  With no enemy fighter anywhere the
        army takes the capture circle and pushes."""
        if not front:
            return
        fighters = self.op_fighters
        if self.PUSH_MODE:
            fighters = self._push_filter(front, fighters)
        if self.LEASH > 0:
            # Payload-centred army (what noeyedeer and JaniceKeepTalking do): only chase
            # enemies near the payload.  Raiders parked on our deposit are ignored and the
            # army keeps pushing; with nobody near the payload it takes the circle.
            px0, py0 = self.P
            L2 = self.LEASH ** 2
            fighters = [e for e in fighters if (e.px - px0) ** 2 + (e.py - py0) ** 2 <= L2]
        if self.home == "race":
            # Leave the raiders on our deposit; they are not worth a corridor fight.  The
            # army escorts the payload: it fights only what comes near it and otherwise
            # sits in the circle pushing (noeyedeer's answer to Gang, match 260).
            px0, py0 = self.P
            L2 = self.RACE_LEASH ** 2
            others = [e for e in fighters if e.id not in self.home_ids]
            near = [e for e in others if (e.px - px0) ** 2 + (e.py - py0) ** 2 <= L2]
            # Safety valve: if a real army gathers around the payload (the raiders coming
            # back, or reinforcements near their spawn), stop racing and fight it properly.
            if sum(self._power(e) for e in near) >= self.RACE_ABORT * max(self.my_near, 1.0):
                fighters = others
            else:
                fighters = near
        elif self.home == "defend":
            if self._home_gather(front, moves):
                return
            fighters = self.home_raid
        # Late and behind on the payload: sitting still is a certain tiebreak loss, so every
        # gun goes for the bodies holding the circle, at close range.
        late_behind = (
            self.LATE_TICKS > 0
            and self.T > self.MAX_T - self.LATE_TICKS
            and self.capture <= 0.0
        )
        if late_behind:
            px, py = self.P
            zr2 = (self.CAP_R + 1.5) ** 2
            holders = [e for e in self.op if (e.px - px) ** 2 + (e.py - py) ** 2 <= zr2]
            if holders:
                fighters = holders
        if self.MOVE_MODE == "slots" or not fighters:
            return self._slot_moves(front, moves)
        D = self.D_PRESS
        D2 = D * D
        shoot2 = (self.RANGE - 0.6) ** 2
        # Tiebreak insurance: armies parked on either side of a wall never shoot, and at
        # max_ticks the payload's side decides.  If it sits on our half, unmoved, with no
        # enemy in the circle, two bots walk in and push it back; ahead, nothing changes.
        if late_behind or (
            self.capture <= 0.02
            and self.T - self.capture_moved > self.STALL_TICKS
            and self.T > 1500
            and not self.op_in_zone
        ):
            front = self._anchor_duty(front, moves)
        px, py = self.P
        # Only in a frozen standoff (payload unmoved for a while): in a live fight the
        # formation must hold.
        sidestep_ok = self.SIDESTEP and self.T - self.capture_moved > self.SIDESTEP_STALL
        zone2 = (self.CAP_R + self.ZONE_MARGIN) ** 2
        Dn2 = self.D_NEAR ** 2
        # Dynamic range: with a clear local edge, close in to finish the fight faster.
        if self.DYN_RANGE and self.my_near >= self.DYN_EDGE * self.op_near + 1.0:
            D2 = self.D_CLOSE ** 2
        under_fire2 = (self.RANGE + 1.5) ** 2
        safe2 = (self.RANGE + 2.5) ** 2
        for b in front:
            ranked = sorted(fighters, key=lambda o: (o.px - b.x) ** 2 + (o.py - b.y) ** 2)
            e = ranked[0]
            d2 = (e.px - b.x) ** 2 + (e.py - b.y) ** 2
            if self.RETREAT_HP > 0:
                # Rotate the badly hurt out of reach: facing is independent of movement, so
                # a retreating bot keeps shooting back while the healers top it up, and the
                # enemy is denied the kill.
                if b.id in self.retreating and b.hp >= self.RETURN_HP:
                    self.retreating.discard(b.id)
                elif b.id not in self.retreating and b.hp <= self.RETREAT_HP and d2 <= under_fire2:
                    self.retreating.add(b.id)
                    self.stats["retreat_enter"] += 1
                if b.id in self.retreating:
                    if d2 < safe2:
                        ax, ay = self._away(b.x, b.y, fighters)
                        moves[b.id] = self._nav(b.x, b.y, b.x + ax * 3.0, b.y + ay * 3.0)
                    else:
                        moves[b.id] = (0.0, 0.0)
                    continue
            # Stand off at long range against an army in the open (it has to walk into our
            # fire), but close in on bodies sitting on the payload: from far away the
            # payload shields them, and they win the tiebreak by just sitting there.
            lim2 = Dn2 if late_behind or (e.px - px) ** 2 + (e.py - py) ** 2 <= zone2 else D2
            if d2 > lim2:
                moves[b.id] = self._nav(b.x, b.y, e.px, e.py)
                continue
            if sidestep_ok and self._payload_blocks(b.x, b.y, e.px, e.py):
                # Everything we could shoot hides behind the payload (a stack sitting on
                # it): nobody fires and the circle stays theirs.  Step sideways out of the
                # payload's shadow instead of standing still.
                clear = False
                for o in ranked[:4]:
                    if (o.px - b.x) ** 2 + (o.py - b.y) ** 2 > shoot2:
                        break
                    if not self._payload_blocks(b.x, b.y, o.px, o.py):
                        clear = True
                        break
                if not clear:
                    lx, ly = e.px - b.x, e.py - b.y
                    n = math.hypot(lx, ly) or 1.0
                    nx, ny = -ly / n, lx / n
                    # Move to the side of the line away from the payload centre.
                    if (px - b.x) * nx + (py - b.y) * ny > 0:
                        nx, ny = -nx, -ny
                    moves[b.id] = self._nav(b.x, b.y, b.x + nx * 1.5, b.y + ny * 1.5)
                    continue
            if self.PRESS_LOS:
                # Stand only where we actually have a shot at someone; otherwise keep
                # walking the route toward the nearest enemy until a line opens.
                clear = False
                for o in ranked[:4]:
                    if (o.px - b.x) ** 2 + (o.py - b.y) ** 2 > shoot2:
                        break
                    if not self._payload_blocks(b.x, b.y, o.px, o.py) and self._los(
                        b.x, b.y, o.px, o.py
                    ):
                        clear = True
                        break
                if not clear:
                    moves[b.id] = self._nav(b.x, b.y, e.px, e.py)
                    continue
            if self.STRAFE:
                # Side-step across the enemy's line of fire, flipping direction every few
                # ticks.  (Experimental: hitscan with a 0.25 hull barely cares.)
                lx, ly = e.px - b.x, e.py - b.y
                n = math.hypot(lx, ly) or 1.0
                sgn = 1.0 if ((self.T // self.STRAFE) + b.id) % 2 else -1.0
                moves[b.id] = (-ly / n * sgn * 0.8, lx / n * sgn * 0.8)
            else:
                moves[b.id] = (0.0, 0.0)

    def _push_filter(self, front, fighters) -> list:
        """Push mode.  When the enemy's main force sits far from the payload (turtling on
        its own deposit like DIBSFA in match 376, or raiding ours like Gang), chasing the
        nearest enemy drags the army away and the circle stays empty.  Then only enemies
        near the payload are pressed and everyone else takes the circle and pushes.  Leave
        as soon as a real force comes back to the payload.  Both switches need the
        condition to hold for a while (hysteresis)."""
        px, py = self.P
        near_r2 = self.PUSH_NEAR ** 2
        op_near = sum(
            self._power(e) for e in fighters if (e.px - px) ** 2 + (e.py - py) ** 2 <= near_r2
        )
        my_near = sum(
            self._power(u)
            for u in self.me
            if u.cls != EXTRACTOR and (u.x - px) ** 2 + (u.y - py) ** 2 <= near_r2
        )
        op_all = self.op_all
        # Only a passive enemy: its main force sitting on ITS OWN deposit.  A raid on ours
        # is a different situation (the baseline answer beats it) and must not trigger.
        hx, hy = self.dep_other
        home_r2 = self.PUSH_HOME_R ** 2
        op_home = sum(
            self._power(e) for e in fighters if (e.px - hx) ** 2 + (e.py - hy) ** 2 <= home_r2
        )
        if not self.push:
            want = (
                self.T > 800
                and op_all > 1.0
                and op_home >= 0.5 * op_all
                and op_near <= 0.6 * self.my_all
            )
            self.push_count = self.push_count + 1 if want else 0
            if self.push_count >= self.PUSH_ENTER:
                self.push, self.push_count = True, 0
                self.stats["push_enter"] = self.stats.get("push_enter", 0) + 1
        else:
            leave = op_near >= 0.8 * max(my_near, 1.0) or op_home < 0.3 * op_all
            self.push_count = self.push_count + 1 if leave else 0
            if self.push_count >= self.PUSH_EXIT:
                self.push, self.push_count = False, 0
        if not self.push:
            return fighters
        self.stats["push_ticks"] = self.stats.get("push_ticks", 0) + 1
        L2 = self.PUSH_LEASH ** 2
        return [e for e in fighters if (e.px - px) ** 2 + (e.py - py) ** 2 <= L2]

    def _anchor_duty(self, front, moves) -> list:
        """Send two battle bots into the capture circle; return the rest."""
        if len(front) < 4:
            return front
        px, py = self.P
        key = (round(px * 4), round(py * 4))
        cache = self.__dict__.get("_anchor_cache")
        if cache and cache[0] == key and self.T - cache[1] < 60:
            spots = cache[2]
        else:
            spots = self._anchor_points(2, [])
            self._anchor_cache = (key, self.T, spots)
        if not spots:
            return front
        order = sorted(front, key=lambda b: (b.x - px) ** 2 + (b.y - py) ** 2)
        chosen = order[: len(spots)]
        for b, (tx, ty) in zip(chosen, spots):
            moves[b.id] = self._nav(b.x, b.y, tx, ty)
        ids = {b.id for b in chosen}
        return [b for b in front if b.id not in ids]

    def _slot_moves(self, front, moves) -> None:
        slots = self._slots(len(front))
        if not slots:
            for b in front:
                moves[b.id] = self._nav(b.x, b.y, self.P[0], self.P[1])
            return
        # Greedy min-distance matching; a bot keeps its slot unless another is much closer.
        prev = self.slot_owner
        pairs = []
        for b in front:
            for k, (sx, sy) in enumerate(slots):
                d = (b.x - sx) ** 2 + (b.y - sy) ** 2
                if prev.get(b.id) == k:
                    d *= 0.5
                pairs.append((d, b.id, k))
        pairs.sort()
        taken_b: set = set()
        taken_s: set = set()
        owner = {}
        for d, bid, k in pairs:
            if bid in taken_b or k in taken_s:
                continue
            taken_b.add(bid)
            taken_s.add(k)
            owner[bid] = k
        self.slot_owner = owner
        for b in front:
            k = owner.get(b.id)
            tx, ty = slots[k] if k is not None else self.P
            moves[b.id] = self._nav(b.x, b.y, tx, ty)

    def _slots(self, n: int) -> list:
        T = self.T
        px, py = self.P
        fx, fy = self.front_c
        key = (
            self.stance,
            n,
            round(px * 2),
            round(py * 2),
            round(fx / 1.5),
            round(fy / 1.5),
            len(self.rel),
        )
        if key == self.slot_key and len(self.slots) >= n and T - self.slot_tick < 30:
            return self.slots
        if self.cheap and self.slots and T - self.slot_tick < 60:
            return self.slots
        if not self.rel:
            slots = self._objective_slots(n)
        elif self.stance == "retreat":
            slots = self._retreat_slots(n)
        else:
            slots = self._engage_slots(n)
        self.slots = slots
        self.slot_key = key
        self.slot_tick = T
        return slots

    def _pick_spread(self, cands, n, chosen=None, los_to=None):
        chosen = list(chosen or [])
        sp2 = self.SPACING ** 2
        start = len(chosen)
        for s, x, y in cands:
            if len(chosen) - start >= n:
                break
            if all((x - a) ** 2 + (y - b) ** 2 >= sp2 for a, b in chosen):
                if los_to is not None and not self._los(x, y, los_to[0], los_to[1]):
                    continue
                chosen.append((x, y))
        return chosen

    def _objective_slots(self, n: int) -> list:
        """No enemy around: take the capture circle and spread on the enemy side of it so
        the payload is behind us, not between us and whoever arrives."""
        px, py = self.P
        ux, uy = self.u
        min_r = self.P_R + self.HULL_SAFE
        anchors = []
        ring = []
        for (x, y) in self.grid:
            dx, dy = x - px, y - py
            r2 = dx * dx + dy * dy
            if r2 > 6.5 * 6.5 or r2 < min_r * min_r:
                continue
            r = math.sqrt(r2)
            side = dx * ux + dy * uy
            if r <= self.CAP_R - 0.3:
                anchors.append((-abs(r - 1.8) - abs(side) * 0.3, x, y))
            ring.append((-abs(r - 3.2) * 0.7 + max(-1.5, min(side, 2.0)) * 0.5, x, y))
        anchors.sort(reverse=True)
        ring.sort(reverse=True)
        chosen = self._pick_spread(anchors[:80], min(3, n), los_to=(px, py))
        chosen = self._pick_spread(ring[:400], n - len(chosen), chosen, los_to=(px, py))
        return self._fill(chosen, n, px, py)

    def _engage_slots(self, n: int) -> list:
        """Firing positions against the enemy front: inside our range of its nearest
        members with a clear line past the payload, spread, not strung across the map."""
        px, py = self.P
        ax, ay = self.AC
        rel = self.rel
        want = self.D_ATTACK if self.stance == "attack" else self.D_HOLD
        lo2 = (want - 2.3) ** 2
        hi2 = (want + 1.6) ** 2
        rng2 = (self.RANGE - 0.4) ** 2
        cap2 = (self.CAP_R - 0.25) ** 2
        min_p2 = (self.P_R + self.HULL_SAFE) ** 2
        # Plan around both the army and the objective so the fight is taken to the circle.
        mx, my = (ax + px) * 0.5, (ay + py) * 0.5
        pts = [(e.px, e.py) for e in rel]
        contest = self.stance != "retreat"
        cands = []
        for (x, y) in self.coarse:
            dmx = x - mx
            dmy = y - my
            dm2 = dmx * dmx + dmy * dmy
            if dm2 > 15.0 ** 2:
                continue
            dn2 = 1e18
            vis = 0
            for (ex, ey) in pts:
                d2 = (x - ex) ** 2 + (y - ey) ** 2
                if d2 < dn2:
                    dn2 = d2
                if d2 <= rng2 and _seg_dist2(px, py, x, y, ex, ey) >= self.P_BLOCK2:
                    vis += 1
            if dn2 < lo2 or dn2 > hi2:
                continue
            dp2 = (x - px) ** 2 + (y - py) ** 2
            if dp2 < min_p2:
                continue
            s = min(vis, 5) * 1.6 - abs(math.sqrt(dn2) - want) * 0.9 - math.sqrt(dm2) * 0.07
            if contest and dp2 <= cap2:
                s += 1.5
            cands.append((s, x, y))
        cands.sort(reverse=True)
        # Wall check against the nearest enemy for the leading candidates only.
        chosen: list = []
        sp2 = self.SPACING ** 2
        for s, x, y in cands[: 6 * n + 20]:
            if len(chosen) >= n:
                break
            if not all((x - a) ** 2 + (y - b) ** 2 >= sp2 for a, b in chosen):
                continue
            ne = min(pts, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
            if not self._los(x, y, ne[0], ne[1]):
                continue
            chosen.append((x, y))
        if len(chosen) < n:
            # Fine-grid fill near the best points (corridors are narrow).
            chosen = self._fill(chosen, n, *(chosen[0] if chosen else (ax, ay)))
        # Anchor the circle: make sure a couple of slots sit inside it when we contest.
        if contest:
            inside = sum(1 for (x, y) in chosen if (x - px) ** 2 + (y - py) ** 2 <= cap2)
            if inside < 2 and n >= 4:
                anchors = self._anchor_points(2 - inside, chosen)
                # Replace the worst (last) slots.
                if anchors:
                    chosen = chosen[: n - len(anchors)] + anchors
        return chosen

    def _anchor_points(self, k, avoid) -> list:
        """Points inside the capture circle least exposed to the enemy front."""
        px, py = self.P
        min_r = self.P_R + self.HULL_SAFE
        cap = self.CAP_R - 0.3
        pts = [(e.px, e.py) for e in self.rel]
        cands = []
        for (x, y) in self.grid:
            dx, dy = x - px, y - py
            r2 = dx * dx + dy * dy
            if r2 < min_r * min_r or r2 > cap * cap:
                continue
            exposed = 0
            for (ex, ey) in pts:
                if (x - ex) ** 2 + (y - ey) ** 2 <= 100.0 and _seg_dist2(
                    px, py, x, y, ex, ey
                ) >= self.P_BLOCK2:
                    exposed += 1
            cands.append((-exposed, x, y))
        cands.sort(reverse=True)
        sp2 = self.SPACING ** 2
        out = []
        for s, x, y in cands:
            if len(out) >= k:
                break
            if all((x - a) ** 2 + (y - b) ** 2 >= sp2 for a, b in list(avoid) + out):
                out.append((x, y))
        return out

    def _retreat_slots(self, n: int) -> list:
        """Out of the enemy's reach, between it and our spawn, so reinforcements join the
        army instead of walking into the enemy one at a time."""
        fx, fy = self.front_c
        # Walk back along our half of the payload track until out of range of the front.
        c = self.capture
        rx, ry = self._payload(c)
        for k in range(0, 40):
            cx, cy = self._payload(c - 0.025 * k)
            nearest = min(
                ((e.px - cx) ** 2 + (e.py - cy) ** 2 for e in self.rel), default=1e9
            )
            rx, ry = cx, cy
            if nearest >= (self.RANGE + 2.5) ** 2:
                break
            if c - 0.025 * k <= -1.0:
                break
        pts = [(e.px, e.py) for e in self.rel]
        cands = []
        for (x, y) in self.coarse:
            d2 = (x - rx) ** 2 + (y - ry) ** 2
            if d2 > 6.0 ** 2:
                continue
            dn = min(((x - ex) ** 2 + (y - ey) ** 2 for ex, ey in pts), default=1e9)
            s = -math.sqrt(d2) * 0.5 + min(math.sqrt(dn), self.RANGE + 2.0) * 0.4
            cands.append((s, x, y))
        cands.sort(reverse=True)
        chosen = self._pick_spread(cands, n, los_to=(rx, ry))
        return self._fill(chosen, n, rx, ry)

    def _fill(self, chosen, n, cx, cy) -> list:
        if len(chosen) >= n:
            return chosen[:n]
        px, py = self.P
        min_p2 = (self.P_R + self.HULL_SAFE) ** 2
        sp2 = self.SPACING ** 2
        rest = sorted(
            ((x - cx) ** 2 + (y - cy) ** 2, x, y)
            for (x, y) in self.grid
            if (x - cx) ** 2 + (y - cy) ** 2 <= 8.0 ** 2
        )
        chosen = list(chosen)
        for _, x, y in rest:
            if len(chosen) >= n:
                break
            if (x - px) ** 2 + (y - py) ** 2 < min_p2:
                continue
            if all((x - a) ** 2 + (y - b) ** 2 >= sp2 for a, b in chosen):
                chosen.append((x, y))
        while len(chosen) < n:
            chosen.append((cx, cy))
        return chosen

    def _battle_facing(self, b, ox, oy) -> float:
        """Idle battle bots pre-aim where the enemy will appear."""
        e = self.op_by_id.get(self.aim.get(b.id, -1))
        if e is not None:
            return _ang(e.px - ox, e.py - oy)
        best = None
        bd = 1e9
        for e in self.op:
            d = (e.x - ox) ** 2 + (e.y - oy) ** 2
            if d < bd:
                bd, best = d, e
        if best is not None and bd < (self.RANGE + 5.0) ** 2:
            return _ang(best.px - ox, best.py - oy)
        px, py = self.P
        ux, uy = self.u
        return _ang(px + ux * 8.0 - ox, py + uy * 8.0 - oy)

    # -------------------------------------------------------------- extractors

    def _extractor_moves(self, extractors, moves) -> dict:
        out = {}
        if not extractors:
            return out
        if self.endgame:
            return self._endgame_extractors(extractors, moves)
        slots = self.mine_slots
        alive = {e.id for e in extractors}
        for bid in list(self.mine_slot):
            if bid not in alive:
                del self.mine_slot[bid]
        used = set(self.mine_slot.values())
        raiders = [e for e in self.raid if e.cls == BATTLE]
        guarded = self.n_guards > 0
        if self.stealing and self.steal_slots:
            # The enemy army sits on our deposit, so theirs is empty: mine it instead.
            ex, ey = self.dep_other
            taken: set = set()
            for u in sorted(extractors, key=lambda e: e.id):
                k = min(
                    (i for i in range(len(self.steal_slots)) if i not in taken),
                    key=lambda i: (self.steal_slots[i][0] - u.x) ** 2 + (self.steal_slots[i][1] - u.y) ** 2,
                    default=u.id % len(self.steal_slots),
                )
                taken.add(k)
                tx, ty = self.steal_slots[k]
                mx, my = self._nav(u.x, u.y, tx, ty)
                moves[u.id] = (mx, my)
                ox, oy = u.x + mx * self.SPEED, u.y + my * self.SPEED
                out[u.id] = (_ang(ex - ox, ey - oy), True)
            return out
        for u in sorted(extractors, key=lambda e: e.id):
            if raiders and not guarded:
                close = [e for e in raiders if (e.x - u.x) ** 2 + (e.y - u.y) ** 2 < 10.5 ** 2]
                if close:
                    fx, fy = self._flee_point(u.x, u.y, close)
                    moves[u.id] = self._nav(u.x, u.y, fx, fy)
                    out[u.id] = (None, True)
                    continue
            k = self.mine_slot.get(u.id)
            if k is None:
                free = [i for i in range(len(slots)) if i not in used]
                if not free:
                    k = u.id % len(slots)
                else:
                    k = min(free, key=lambda i: (slots[i][0] - u.x) ** 2 + (slots[i][1] - u.y) ** 2)
                self.mine_slot[u.id] = k
                used.add(k)
            tx, ty = slots[k]
            moves[u.id] = self._nav(u.x, u.y, tx, ty)
            out[u.id] = (None, True)  # always ask to mine; the engine checks the ray
        return out

    def _endgame_extractors(self, extractors, moves) -> dict:
        """Tokens are worthless now.  Extractors become bodies: they keep the capture
        circle contested when the army cannot, and one always hides to avoid a wipe."""
        out = {}
        px, py = self.P
        ux, uy = self.u
        need_bodies = len([u for u in self.me_in_zone if u.cls != EXTRACTOR]) < 2
        hide = max(
            extractors,
            key=lambda e: min(((e.x - o.x) ** 2 + (e.y - o.y) ** 2 for o in self.op), default=1e9),
        )
        k = 0
        for u in sorted(extractors, key=lambda e: e.id):
            if u.id == hide.id and (len(extractors) > 1 or not need_bodies):
                tx, ty = self._hideout()
            elif need_bodies:
                ang = math.atan2(-uy, -ux) + (k - len(extractors) / 2.0) * 0.55
                tx = px + math.cos(ang) * 1.9
                ty = py + math.sin(ang) * 1.9
                k += 1
            else:
                ang = math.atan2(-uy, -ux) + (k - len(extractors) / 2.0) * 0.35
                tx = px + math.cos(ang) * 5.0
                ty = py + math.sin(ang) * 5.0
                k += 1
            moves[u.id] = self._nav(u.x, u.y, tx, ty)
            out[u.id] = (None, False)
        return out

    def _hideout(self):
        """A standable point as far as possible from every enemy (checked coarsely)."""
        if not self.op:
            return self.spawn[0] + 1.0, self.spawn[1] - 1.0
        best = None
        bs = -1.0
        for (x, y) in self.coarse[::3]:
            d = min((x - e.x) ** 2 + (y - e.y) ** 2 for e in self.op)
            if d > bs:
                bs, best = d, (x, y)
        return best

    # ----------------------------------------------------------------- healers

    def _healer_moves(self, healers, me, moves) -> dict:
        """Pair healers with patients (globally, by how soon the heal can land), then
        place them.  Returns {healer id: patient id or None}; the trigger is decided in
        `_heal_triggers` once separation has fixed the final moves."""
        plans: dict = {}
        if not healers:
            return plans
        maxhp = self.MAXHP
        reach2 = (self.HEAL_R - 0.15) ** 2
        cone = self.HEAL_HALF + self.TURN - 2.0
        wounded = [a for a in me if a.hp < maxhp - 0.1]
        ax, ay = self.AC
        battles_alive = any(u.cls == BATTLE for u in me)
        enemies = self.op_fighters
        pairs = []
        for h in healers:
            prev = self.heal_target.get(h.id)
            for a in wounded:
                if a.id == h.id:
                    continue
                ex, ey = a.x + a.vx, a.y + a.vy
                dx, dy = ex - h.x, ey - h.y
                d2 = dx * dx + dy * dy
                if d2 > 12.0 ** 2:
                    continue
                d = math.sqrt(d2)
                missing = maxhp - a.hp
                s = missing + (1.5 if a.cls == BATTLE else 0.5 if a.cls == HEALER else 0.0)
                now = d2 <= reach2 and abs(_adiff(_ang(dx, dy), h.ang)) <= cone
                if now:
                    s += 12.0
                else:
                    s -= max(0.0, d - (self.HEAL_R - 0.4)) * 1.4
                if a.id == prev:
                    s += 2.0
                pairs.append((s, h.id, a.id, now))
        pairs.sort(key=lambda t: -t[0])
        stack: dict[int, int] = {}
        chosen: dict[int, tuple] = {}
        for s, hid, aid, now in pairs:
            if hid in chosen or stack.get(aid, 0) >= self.STACK:
                continue
            chosen[hid] = (aid, now, stack.get(aid, 0))
            stack[aid] = stack.get(aid, 0) + 1
        by_id = {u.id: u for u in me}
        order = sorted(healers, key=lambda h: h.id)
        for idx, h in enumerate(order):
            pick = chosen.get(h.id)
            if pick is None or not battles_alive:
                if battles_alive:
                    bx, by = self._away(ax, ay, enemies)
                    off = (idx - (len(order) - 1) / 2.0) * 1.1
                    tx = ax + bx * 2.4 - by * off
                    ty = ay + by * 2.4 + bx * off
                else:
                    tx, ty = self._hideout()
                moves[h.id] = self._nav(h.x, h.y, tx, ty)
            if pick is None:
                self.heal_target.pop(h.id, None)
                plans[h.id] = None
                continue
            aid, now, slot = pick
            a = by_id[aid]
            self.heal_target[h.id] = aid
            plans[h.id] = aid
            if not battles_alive:
                continue
            bx, by = self._away(a.x, a.y, enemies)
            d = math.hypot(a.x - h.x, a.y - h.y)
            # Are we standing in front of the patient (closer to the enemy)?
            ahead = (h.x - a.x) * bx + (h.y - a.y) * by < -0.3
            if now and not ahead and 1.2 <= d <= self.HEAL_R - 0.45:
                # Already in a good spot: just track the patient's drift.
                moves[h.id] = (a.vx / self.SPEED * 0.8, a.vy / self.SPEED * 0.8)
                continue
            lat = (slot - 1) * 1.05 if slot else 0.0
            tx = a.x + bx * 1.9 - by * lat
            ty = a.y + by * 1.9 + bx * lat
            if getattr(self, "HEAL_SPOT", 0) and not self._heal_spot_ok(tx, ty, a):
                base = math.atan2(ty - a.y, tx - a.x)
                radius = max(1.4, math.hypot(tx - a.x, ty - a.y))
                for direction in (1, -1, 2, -2, 3, -3, 4, -4):
                    angle = base + direction * 0.45
                    candidate_x = a.x + math.cos(angle) * radius
                    candidate_y = a.y + math.sin(angle) * radius
                    if self._heal_spot_ok(candidate_x, candidate_y, a):
                        tx, ty = candidate_x, candidate_y
                        break
            moves[h.id] = self._nav(h.x, h.y, tx, ty)
        return plans

    def _heal_spot_ok(self, x, y, patient) -> bool:
        return self._disc_free(x, y, self.R + 0.05) and self._los(x, y, patient.x, patient.y)

    def _heal_triggers(self, healers, plans, moves) -> dict:
        """Final heading and trigger per healer, with the post-separation move."""
        out = {}
        ax, ay = self.AC
        by_id = {u.id: u for u in self.me}
        landed = {}
        misses = []
        for h in healers:
            mx, my = moves.get(h.id, (0.0, 0.0))
            ox = h.x + mx * self.SPEED
            oy = h.y + my * self.SPEED
            aid = plans.get(h.id)
            a = by_id.get(aid) if aid is not None else None
            if a is None:
                out[h.id] = (0, _ang(ax - ox, ay - oy), False)
                if STATS:
                    self.stats["miss_none"] = self.stats.get("miss_none", 0) + 1
                continue
            ex, ey = a.x + a.vx, a.y + a.vy
            want = _ang(ex - ox, ey - oy)
            turn = max(-self.TURN, min(self.TURN, _adiff(want, h.ang)))
            after = h.ang + turn
            d = math.hypot(ex - ox, ey - oy)
            ok = (
                d <= self.HEAL_R - 0.03
                and abs(_adiff(want, after)) <= self.HEAL_HALF - 1.0
                and self._los(ox, oy, ex, ey)
            )
            out[h.id] = (a.id, want, ok)
            if ok:
                landed[a.id] = landed.get(a.id, 0) + 1
            else:
                misses.append((h, ox, oy))
            if STATS:
                self.stats["heal_try"] += 1
                self.stats["heal_ok"] += 1 if ok else 0
        if getattr(self, "HEAL_ANY", 0) and misses:
            wounded = [u for u in self.me if u.hp < self.MAXHP - 0.05]
            for h, ox, oy in misses:
                best = None
                for a in wounded:
                    if a.id == h.id or landed.get(a.id, 0) >= self.STACK:
                        continue
                    ex, ey = a.x + a.vx, a.y + a.vy
                    d = math.hypot(ex - ox, ey - oy)
                    if d > self.HEAL_R - 0.03:
                        continue
                    want = _ang(ex - ox, ey - oy)
                    turn = max(-self.TURN, min(self.TURN, _adiff(want, h.ang)))
                    if abs(_adiff(want, h.ang + turn)) > self.HEAL_HALF - 1.0:
                        continue
                    score = self.MAXHP - a.hp + (1.0 if a.cls == BATTLE else 0.0)
                    if best is None or score > best[0]:
                        best = (score, a, want, ex, ey)
                if best is None:
                    continue
                _, a, want, ex, ey = best
                if not self._los(ox, oy, ex, ey):
                    continue
                out[h.id] = (a.id, want, True)
                landed[a.id] = landed.get(a.id, 0) + 1
                if STATS:
                    self.stats["heal_alt"] = self.stats.get("heal_alt", 0) + 1
        return out

    def _away(self, x, y, enemies):
        """Unit vector pointing away from the nearby enemies (or back from the front)."""
        sx = sy = 0.0
        for e in enemies:
            dx, dy = x - e.x, y - e.y
            d2 = dx * dx + dy * dy
            if d2 < 14.0 ** 2 and d2 > 1e-6:
                k = 1.0 / d2
                sx += dx * k
                sy += dy * k
        n = math.hypot(sx, sy)
        if n < 1e-9:
            ux, uy = self.u
            return -ux, -uy
        return sx / n, sy / n

    # -------------------------------------------------------------- separation

    def _separate(self, me, moves) -> None:
        sp = self.SPACING
        sp2 = sp * sp
        px, py = self.P
        hull_p = self.P_R + self.HULL_SAFE
        dx0, dy0 = self.dep
        hull_d = self.DEP_R + self.HULL_SAFE
        n = len(me)
        push = {u.id: [0.0, 0.0] for u in me}
        for i in range(n):
            a = me[i]
            for j in range(i + 1, n):
                b = me[j]
                dx = a.x - b.x
                dy = a.y - b.y
                d2 = dx * dx + dy * dy
                if d2 >= sp2:
                    continue
                if d2 < 1e-6:
                    ang = (a.id * 2.399963) % (2 * math.pi)
                    dx, dy, d = math.cos(ang), math.sin(ang), 1e-3
                else:
                    d = math.sqrt(d2)
                    dx /= d
                    dy /= d
                k = (sp - d) / sp
                pa = push[a.id]
                pb = push[b.id]
                pa[0] += dx * k
                pa[1] += dy * k
                pb[0] -= dx * k
                pb[1] -= dy * k
        for u in me:
            mx, my = moves.get(u.id, (0.0, 0.0))
            rx, ry = push[u.id]
            # Keep off solid hulls so a shot into the payload/deposit cannot splash us.
            for cx, cy, lim, skip in ((px, py, hull_p, False), (dx0, dy0, hull_d, u.cls == EXTRACTOR)):
                if skip:
                    continue
                dx, dy = u.x - cx, u.y - cy
                d = math.hypot(dx, dy)
                if 1e-6 < d < lim:
                    k = (lim - d) / lim * 2.0
                    rx += dx / d * k
                    ry += dy / d * k
            vx = mx + rx * 1.6
            vy = my + ry * 1.6
            nrm = math.hypot(vx, vy)
            if nrm > 1.0:
                vx /= nrm
                vy /= nrm
            moves[u.id] = (vx, vy)

    # ================================================================== combat

    def _enemy_value(self, e) -> float:
        px, py = self.P
        v = 10.0
        if e.cls == HEALER:
            v += 5.0
        elif e.cls == EXTRACTOR:
            v -= 3.0
            if self.raiders:
                ex, ey = self.dep_other
                if (e.x - ex) ** 2 + (e.y - ey) ** 2 < 7.0 ** 2:
                    v += 9.0
        if (e.x - px) ** 2 + (e.y - py) ** 2 <= (self.CAP_R + 0.4) ** 2:
            v += 5.0
        v += (self.MAXHP - e.hp) * 0.9
        if e.hp <= self.DMG + 0.01:
            v += 5.0
        return v

    def _assign_aims(self, battles, op) -> None:
        T = self.T
        if not op:
            self.aim = {}
            return
        if self.cheap and T % 4:
            return
        values = {e.id: self._enemy_value(e) for e in op}
        count: dict[int, int] = {}
        new_aim = {}
        rng2 = (self.RANGE + 1.0) ** 2
        order = sorted(battles, key=lambda b: b.nft)
        for b in order:
            cands = []
            for e in op:
                dx = e.px - b.x
                dy = e.py - b.y
                d2 = dx * dx + dy * dy
                if d2 > rng2:
                    continue
                if self._payload_blocks(b.x, b.y, e.px, e.py):
                    continue
                d = math.sqrt(d2)
                turn = abs(_adiff(_ang(dx, dy), b.ang)) / self.TURN
                wait = max(turn, b.nft - T, e.inv - T, 0)
                s = values[e.id] - 0.14 * wait - 0.2 * d
                if self.aim.get(b.id) == e.id:
                    s += 2.5
                k = count.get(e.id, 0)
                need = int(e.hp / self.DMG + 0.999)
                if k >= need:
                    s -= 6.0 * (k - need + 1)
                cands.append((s, e))
            if not cands:
                continue
            cands.sort(key=lambda t: -t[0])
            for s, e in cands[:4]:
                if self._los(b.x, b.y, e.px, e.py):
                    new_aim[b.id] = e.id
                    count[e.id] = count.get(e.id, 0) + 1
                    break
        self.aim = new_aim

    def _fire_control(self, battles, op, moves) -> dict:
        """Decide each battle bot's heading and trigger for this tick.

        Returns {id: (heading, fire)}.  Fire is only set when an exact replay of the
        engine's ray says it lands on a vulnerable enemy that no earlier shooter in this
        volley has claimed.
        """
        T = self.T
        out = {}
        if not op:
            return out
        claimed: set = set()
        spd = self.SPEED
        ready = []
        for b in battles:
            mx, my = moves.get(b.id, (0.0, 0.0))
            ox = b.x + mx * spd
            oy = b.y + my * spd
            e = self.op_by_id.get(self.aim.get(b.id, -1))
            if e is not None:
                want = _ang(e.px - ox, e.py - oy)
            else:
                want = self._battle_facing(b, ox, oy)
            turn = max(-self.TURN, min(self.TURN, _adiff(want, b.ang)))
            after = (b.ang + turn) % 360.0
            if b.nft <= T:
                ready.append((b, ox, oy, want, after))
            out[b.id] = (want, False)

        for b, ox, oy, want, after in ready:
            victims = self._shot_victims(ox, oy, after, op, claimed)
            if not victims:
                alt = self._snap_shot(b, ox, oy, op, claimed)
                if alt is None:
                    if DEBUG:
                        self._why_not(b, ox, oy, after, op)
                    continue
                want, after, victims = alt
            for v in victims:
                claimed.add(v)
            out[b.id] = (want, True)
        if STATS:
            fired = {bid for bid, (_, go) in out.items() if go}
            for b, ox, oy, want, after in ready:
                if b.id in fired:
                    self.stats["shots"] += 1
                    continue
                self.stats["ready_idle"] += 1
                e = self.op_by_id.get(self.aim.get(b.id, -1))
                if e is None:
                    continue
                if self._payload_blocks(ox, oy, e.px, e.py):
                    self.stats["blocked_payload"] += 1
                elif not self._los(ox, oy, e.px, e.py):
                    self.stats["blocked_wall"] += 1
        return out

    def _why_not(self, b, ox, oy, after, op) -> None:
        st = self.why
        e = self.op_by_id.get(self.aim.get(b.id, -1))
        if e is None:
            near = min(((x.x - ox) ** 2 + (x.y - oy) ** 2 for x in op), default=1e9)
            key = "no_target_inrange" if near > self.RANGE ** 2 else "no_target_blocked"
        else:
            want = _ang(e.px - ox, e.py - oy)
            if abs(_adiff(want, after)) > 2.5:
                key = "turning"
            elif e.inv > self.T:
                key = "target_invuln"
            else:
                dx = math.cos(after * RAD)
                dy = math.sin(after * RAD)
                tp = _ray_circle(ox, oy, dx, dy, self.P[0], self.P[1], self.P_R)
                te = _ray_circle(ox, oy, dx, dy, e.px, e.py, self.R)
                if tp is not None and (te is None or tp < te):
                    key = "payload_block"
                elif te is None:
                    key = "miss_geom"
                else:
                    key = "other(wall/claim)"
        st[key] = st.get(key, 0) + 1

    def _snap_shot(self, b, ox, oy, op, claimed):
        best = None
        rng2 = self.RANGE ** 2
        for e in op:
            if e.inv > self.T or e.id in claimed:
                continue
            dx, dy = e.px - ox, e.py - oy
            if dx * dx + dy * dy > rng2:
                continue
            want = _ang(dx, dy)
            if abs(_adiff(want, b.ang)) > self.TURN:
                continue
            after = (b.ang + max(-self.TURN, min(self.TURN, _adiff(want, b.ang)))) % 360.0
            victims = self._shot_victims(ox, oy, after, op, claimed)
            if victims:
                val = sum(self._enemy_value(self.op_by_id[v]) for v in victims)
                if best is None or val > best[0]:
                    best = (val, want, after, victims)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def _shot_victims(self, ox, oy, heading, op, claimed):
        """Enemy ids this shot would damage, requiring agreement between the predicted
        (pos + vel) and the stationary (pos) enemy layouts so a jink cannot waste it."""
        dx = math.cos(heading * RAD)
        dy = math.sin(heading * RAD)
        r = self.R
        t_static = min(self.RANGE, _ray_boundary(ox, oy, dx, dy, self.SIZE))
        px, py = self.P
        t = _ray_circle(ox, oy, dx, dy, px, py, self.P_R)
        if t is not None and t < t_static:
            t_static = t
        for cx, cy in (self.dep, self.dep_other):
            t = _ray_circle(ox, oy, dx, dy, cx, cy, self.DEP_R)
            if t is not None and t < t_static:
                t_static = t
        s2 = self.SPLASH ** 2 - 0.004
        result = None
        for mode in (0, 1):
            best_t = t_static
            for e in op:
                ex, ey = (e.px, e.py) if mode == 0 else (e.x, e.y)
                t = _ray_circle(ox, oy, dx, dy, ex, ey, r)
                if t is not None and t < best_t:
                    best_t = t
            ix = ox + dx * best_t
            iy = oy + dy * best_t
            vic = set()
            for e in op:
                if e.inv > self.T or e.id in claimed:
                    continue
                ex, ey = (e.px, e.py) if mode == 0 else (e.x, e.y)
                if (ex - ix) ** 2 + (ey - iy) ** 2 <= s2:
                    vic.add(e.id)
            if not vic:
                return None
            if mode == 0:
                back = max(0.0, best_t - 0.02)
                if not self._los(ox, oy, ox + dx * back, oy + dy * back):
                    return None
                result = vic
            else:
                result = result & vic
        return result or None

    # ================================================================ fallback

    def _fallback(self, state: GameState) -> FleetAction:
        """Minimal but live controller used only if the main one raises: battle bots walk to
        the payload, face the nearest enemy and fire when roughly on target with a line of
        sight; healers heal the nearest wounded ally in reach; extractors mine."""
        act = FleetAction.new()
        conf = get_config()
        T = state.tick
        act.fabricator_next = BATTLE
        act.rush_order = (
            T < conf.max_ticks - conf.endgame_ticks
            and not state.fleet_me.is_full()
            and state.fabricator_me.tokens >= conf.fabricator.rush_cost
        )
        payload = state.payload_pos()
        enemies = list(state.fleet_other)
        allies = list(state.fleet_me)
        rng2 = conf.bot.blaster_range ** 2
        for bot in allies:
            ba = act.bots[bot.id]
            tag = bot.special.tag
            if tag == EXTRACTOR:
                ba.turn_action = turn_towards(state.deposit_me.pos)
                ba.special_action = SpecialAction.Extractor(mine=True)
                ba.move_action = move_bot(navigate_to(bot.pos, state.deposit_me.pos))
                continue
            ba.move_action = move_bot(navigate_to(bot.pos, payload))
            if tag == HEALER:
                hurt = [a for a in allies if a.id != bot.id and a.health < conf.bot.health - 0.1]
                a = min(hurt, key=lambda o: bot.pos.dist_sq(o.pos), default=None)
                if a is None:
                    ba.special_action = SpecialAction.Healer(fire=False, target=0)
                    continue
                ba.turn_action = turn_towards(a.pos)
                off = abs(diff_degrees((a.pos - bot.pos).angle_deg(), bot.angle))
                ok = bot.pos.dist(a.pos) <= conf.bot.base_heal_range - 0.1 and off <= 40.0
                ba.special_action = SpecialAction.Healer(fire=ok, target=a.id)
                continue
            enemy = min(enemies, key=lambda o: bot.pos.dist_sq(o.pos), default=None)
            fire = False
            if enemy is not None:
                ba.turn_action = turn_towards(enemy.pos)
                off = abs(diff_degrees((enemy.pos - bot.pos).angle_deg(), bot.angle))
                fire = (
                    bot.next_fire_tick <= T
                    and bot.pos.dist_sq(enemy.pos) <= rng2
                    and off <= 2.0
                    and enemy.invulnerable_until_tick <= T
                    and line_of_sight(bot.pos, enemy.pos)
                )
            ba.special_action = SpecialAction.Battle(fire=fire)
        return act


class DefensiveAssaultPrototype(AdvancedStrategy):
    """Economy-first defence followed by a healer-backed late assault.

    The base controller has strong fire replay and map helpers, but its default
    movement is payload-first.  This controller deliberately keeps the army at
    home until the late phase, then attacks with a radial keep-away/close-in
    posture instead of parking in a firing line.
    """

    # The endgame starts at tick 6000 with this match config.  Begin the march
    # 1200 ticks earlier so the army reaches the payload with time to fight.
    ASSAULT_PREP_TICKS = _env("IAMABOT_NEW_ASSAULT_PREP", 1200)
    TARGET_EXTRACTORS = _env("IAMABOT_NEW_EXTRACTORS", 6)
    TARGET_HEALERS = _env("IAMABOT_NEW_HEALERS", 8)
    HOME_GUARDS = _env("IAMABOT_NEW_HOME_GUARDS", 4)
    DEFENSE_RADIUS = _env("IAMABOT_NEW_DEFENSE_RADIUS", 13.0)
    DEFENSE_RING = _env("IAMABOT_NEW_DEFENSE_RING", 4.0)

    # Radial combat band.  The bot is never asked to stand at point blank and
    # never intentionally waits outside its ten-tile blaster range.
    KITE_MIN = _env("IAMABOT_NEW_KITE_MIN", 5.9)
    KITE_MAX = _env("IAMABOT_NEW_KITE_MAX", 9.0)
    KITE_NEAR = _env("IAMABOT_NEW_KITE_NEAR", 6.7)
    KITE_FAR = _env("IAMABOT_NEW_KITE_FAR", 8.7)
    KITE_PERIOD = _env("IAMABOT_NEW_KITE_PERIOD", 48)
    KITE_LANE = _env("IAMABOT_NEW_KITE_LANE", 0.45)
    ASSAULT_PAYLOAD_RADIUS = _env("IAMABOT_NEW_ASSAULT_PAYLOAD_RADIUS", 19.0)
    HOME_ALERT_RADIUS = _env("IAMABOT_NEW_HOME_ALERT_RADIUS", 16.0)
    HOME_CRITICAL_RADIUS = _env("IAMABOT_NEW_HOME_CRITICAL_RADIUS", 10.5)
    HOME_ASSAULT_GUARDS = _env("IAMABOT_NEW_HOME_ASSAULT_GUARDS", 2)
    ASSAULT_CRITICAL_GUARDS = _env("IAMABOT_NEW_ASSAULT_CRITICAL_GUARDS", 1)
    HOME_HEALERS = _env("IAMABOT_NEW_HOME_HEALERS", 2)
    PUSHERS = _env("IAMABOT_NEW_PUSHERS", 4)
    HEAL_ANY = _env("IAMABOT_NEW_HEAL_ANY", 1)
    HEAL_SPOT = _env("IAMABOT_NEW_HEAL_SPOT", 1)
    CONVERT = _env("IAMABOT_NEW_CONVERT", 1)
    CONVERT_WINDOW = _env("IAMABOT_NEW_CONVERT_WINDOW", 30)
    CONVERT_KEEP = _env("IAMABOT_NEW_CONVERT_KEEP", 1)

    def __init__(self) -> None:
        super().__init__()
        self.phase = "defense"
        self._home_slots_cache = {}
        self._payload_slots_cache = None
        self.home_guard_ids = set()
        self.home_healer_ids = set()
        self.home_threat = []
        self.home_anchor = "deposit"
        self.payload_pusher_ids = set()

    # --------------------------------------------------------------- phase policy

    def _set_phase(self, tick: int) -> None:
        assault_tick = max(0, self.END_T - self.ASSAULT_PREP_TICKS)
        if tick < assault_tick:
            self.phase = "defense"
        elif tick < self.END_T:
            self.phase = "prep"
        else:
            self.phase = "assault"

    def _tick(self, state: GameState) -> FleetAction:
        # __call__ initializes END_T before dispatching to this method.
        self._set_phase(state.tick)
        act = super()._tick(state)
        if self.CONVERT:
            self._endgame_convert(state, act, list(state.fleet_me))
        return act

    def _endgame_convert(self, state: GameState, act: FleetAction, me: list) -> None:
        """Trade late extractors for Battle bots before the fabricator shuts down.

        Tokens and mining have no value in the endgame.  A full fleet cannot build a
        replacement and self-destruct in the same action, so one extractor is released
        per tick and the next tick's rush fills that slot with a Battle bot.
        """
        if not (self.END_T - self.CONVERT_WINDOW <= state.tick < self.END_T - 1):
            return
        act.fabricator_next = BATTLE
        cost = self.conf.fabricator.rush_cost
        rushing = bool(act.rush_order)
        if state.fabricator_me.tokens - (cost if rushing else 0) < cost:
            return
        if BOTS_MAX - len(me) - (1 if rushing else 0) > 0:
            return
        extractors = [u for u in me if u.special.tag == EXTRACTOR]
        if len(extractors) <= self.CONVERT_KEEP:
            return
        victim = min(
            extractors,
            key=lambda u: min(
                ((u.pos.x - e.pos.x) ** 2 + (u.pos.y - e.pos.y) ** 2 for e in state.fleet_other),
                default=1e9,
            ),
        )
        act.bots[victim.id].self_destruct = True

    def _next_class(self, counts, tick) -> int:
        """Fill the home economy, then maintain a real medical line."""
        nb, nh, ne = counts
        if self.phase in ("defense", "prep"):
            if ne < min(self.TARGET_EXTRACTORS, int(self.conf.deposit.extractor_cap)):
                return EXTRACTOR
            if nh < self.TARGET_HEALERS:
                return HEALER
            return BATTLE

        # Existing extractors cannot be converted by the engine.  New production
        # is therefore only Battle or Healer once the assault has started.
        if nh < self.TARGET_HEALERS:
            return HEALER
        return BATTLE

    def _production(self, state: GameState, act: FleetAction, me: list) -> None:
        counts = [0, 0, 0]
        for unit in me:
            counts[unit.cls] += 1
        act.fabricator_next = self._next_class(counts, state.tick)
        act.rush_order = (
            state.tick < self.END_T
            and len(me) < BOTS_MAX
            and state.fabricator_me.tokens >= self.conf.fabricator.rush_cost
        )

    # -------------------------------------------------------------- home defence

    def _pick_guards(self, battles, op) -> dict:
        """Keep a real reserve at home while the rest marches to the payload.

        The base controller stops assigning guards at the endgame boundary.  That is
        reasonable for a payload-first bot, but it creates a predictable hole for this
        strategy: the old army can arrive at our deposit at exactly the moment our
        extractors leave.  Re-evaluate the home threat every tick and only release the
        reserve when the enemy is no longer near either home objective.
        """
        dx, dy = self.dep
        sx, sy = self.spawn
        alert2 = self.HOME_ALERT_RADIUS ** 2
        critical2 = self.HOME_CRITICAL_RADIUS ** 2
        dep_threat = [
            e for e in self.op_fighters
            if (e.px - dx) ** 2 + (e.py - dy) ** 2 <= alert2
        ]
        spawn_threat = [
            e for e in self.op_fighters
            if (e.px - sx) ** 2 + (e.py - sy) ** 2 <= alert2
        ]
        threat = {e.id: e for e in dep_threat + spawn_threat}
        self.home_threat = list(threat.values())
        self.raid = dep_threat
        self.home_raid = dep_threat
        self.home_ids = {e.id for e in dep_threat}

        alive = {b.id for b in battles}
        self.home_guard_ids.intersection_update(alive)
        if not battles:
            self.home_guard_ids.clear()
            self.home_anchor = "deposit"
            return {}
        if self.phase == "defense":
            # Preserve the baseline's proven raid detector during the long opening.
            # The custom reserve is only needed once the late march starts; replacing
            # the detector here would leave the deposit undefended against an early
            # push and changes the whole opening matchup.
            self.home_guard_ids.clear()
            return super()._pick_guards(battles, op)

        dep_power = sum(self._power(e) for e in dep_threat)
        spawn_power = sum(self._power(e) for e in spawn_threat)
        critical = any(
            (e.px - dx) ** 2 + (e.py - dy) ** 2 <= critical2
            or (e.px - sx) ** 2 + (e.py - sy) ** 2 <= critical2
            for e in self.home_threat
        )
        self.home_critical = critical
        if spawn_power > dep_power * 1.15:
            self.home_anchor = "spawn"
        else:
            self.home_anchor = "deposit"

        if self.home_threat:
            # A critical raid gets enough guns to actually clear the home objective;
            # a distant approach only receives a screening force.  The reserve is
            # deliberately capped by phase: a home raid cannot turn the endgame into
            # a permanent base defence while the payload is still being lost.
            enemy_power = sum(self._power(e) for e in self.home_threat)
            if self.phase == "assault":
                # In the endgame mining is already disabled.  A threat at the old
                # resource point must not strand a full firing line at home; keep only
                # one emergency sentry for a point-blank breach.
                guard_count = min(
                    len(battles),
                    self.ASSAULT_CRITICAL_GUARDS if critical else self.HOME_ASSAULT_GUARDS,
                )
                max_guards = guard_count
            elif self.phase == "prep":
                max_guards = max(1, len(battles) // 2)
            else:
                max_guards = max(1, len(battles) - 4)
            if self.phase != "assault":
                wanted = max(self.HOME_GUARDS, int(math.ceil(enemy_power * (1.25 if critical else 0.9))))
                guard_count = min(max_guards, wanted)
        elif self.phase == "prep":
            guard_count = min(len(battles), max(1, self.HOME_GUARDS))
        else:
            # Once the march reaches the payload, keep only a small sentry pair unless
            # a home threat reappears.  They are enough to catch a lone backdoor unit
            # without sacrificing the late assault's firing mass.
            guard_count = min(len(battles), max(0, self.HOME_ASSAULT_GUARDS))

        previous = self.home_guard_ids
        ordered = sorted(
            battles,
            key=lambda b: (
                0 if b.id in previous else 1,
                min((b.x - dx) ** 2 + (b.y - dy) ** 2, (b.x - sx) ** 2 + (b.y - sy) ** 2),
                b.id,
            ),
        )
        chosen = ordered[:guard_count]
        self.home_guard_ids = {b.id for b in chosen}

        dep_count = len(chosen)
        if spawn_threat:
            spawn_count = min(len(chosen), max(1, int(math.ceil(len(chosen) * 0.4))))
        else:
            spawn_count = min(len(chosen), 1 if len(chosen) >= 6 else 0)
        dep_count -= spawn_count
        dep_slots = self._home_slots("deposit", max(1, dep_count)) if dep_count else []
        spawn_slots = self._home_slots("spawn", max(1, spawn_count)) if spawn_count else []

        targets = {}
        di = si = 0
        for b in chosen:
            use_spawn = si < spawn_count
            slots = spawn_slots if use_spawn else dep_slots
            index = si if use_spawn else di
            if use_spawn:
                si += 1
            else:
                di += 1
            if slots:
                targets[b.id] = slots[index % len(slots)]
            else:
                targets[b.id] = self.spawn if use_spawn else self.dep
        return targets

    def _home_slots(self, name: str, count: int) -> list:
        """Return separated legal positions around the deposit or spawn."""
        key = (name, count)
        cached = self._home_slots_cache.get(key)
        if cached:
            return cached

        if name == "deposit":
            ax, ay = self.dep
            low, high = 1.4, 7.0
        else:
            ax, ay = self.spawn
            low, high = 1.2, 6.2

        # Face the arena so the ring intercepts a raid before it reaches the
        # objective.  Positions are checked against walls and solid objectives.
        vx, vy = self.P[0] - ax, self.P[1] - ay
        length = math.hypot(vx, vy) or 1.0
        vx, vy = vx / length, vy / length
        candidates = []
        for x, y in self.grid:
            dx, dy = x - ax, y - ay
            distance = math.hypot(dx, dy)
            if distance < low or distance > high:
                continue
            if math.hypot(x - self.dep[0], y - self.dep[1]) < self.DEP_R + self.HULL_SAFE:
                continue
            if math.hypot(x - self.P[0], y - self.P[1]) < self.P_R + self.HULL_SAFE:
                continue
            facing = (dx * vx + dy * vy) / distance
            score = -abs(distance - self.DEFENSE_RING) + 0.7 * facing
            candidates.append((score, x, y))
        candidates.sort(reverse=True)

        selected = []
        for _, x, y in candidates:
            if all((x - sx) ** 2 + (y - sy) ** 2 >= self.SPACING ** 2 for sx, sy in selected):
                selected.append((x, y))
            if len(selected) >= count:
                break
        if not selected:
            selected = [(ax + vx * 2.0, ay + vy * 2.0)]
        self._home_slots_cache[key] = selected
        return selected

    def _local_enemy(self, anchor, radius: float, bot=None):
        ax, ay = anchor
        enemies = [
            enemy for enemy in self.op_fighters
            if (enemy.px - ax) ** 2 + (enemy.py - ay) ** 2 <= radius * radius
        ]
        if not enemies:
            return None
        if bot is None:
            return min(enemies, key=lambda e: (e.px - ax) ** 2 + (e.py - ay) ** 2)
        return min(enemies, key=lambda e: (e.px - bot.x) ** 2 + (e.py - bot.y) ** 2)

    def _kite_move(self, bot, enemy, index: int, count: int):
        """Keep an enemy in blaster range with alternating radial movement."""
        dx, dy = bot.x - enemy.px, bot.y - enemy.py
        distance = math.hypot(dx, dy)
        if distance < 1e-4:
            angle = (bot.id * 2.399963) % (2.0 * math.pi)
            ux, uy = math.cos(angle), math.sin(angle)
        else:
            ux, uy = dx / distance, dy / distance
        sx, sy = -uy, ux

        if distance < self.KITE_MIN:
            # Back out immediately from point-blank splash trading.
            tx = bot.x + ux * 3.0
            ty = bot.y + uy * 3.0
        elif distance > self.KITE_MAX:
            # Close until the target is inside the useful band.
            tx = enemy.px + ux * self.KITE_FAR
            ty = enemy.py + uy * self.KITE_FAR
        else:
            # Forward/backward pull alternates per unit so the army does not
            # oscillate as one stack.  A small lane offset creates crossfire.
            radial_phase = ((self.T + bot.id * 17) // self.KITE_PERIOD) & 1
            desired = self.KITE_NEAR if radial_phase == 0 else self.KITE_FAR
            lane = ((index % 5) - 2) * self.KITE_LANE
            tx = enemy.px + ux * desired + sx * lane
            ty = enemy.py + uy * desired + sy * lane
        return self._nav(bot.x, bot.y, tx, ty)

    def _enemy_value(self, enemy) -> float:
        # The baseline's healers are the reason a numerically even late fight
        # becomes a war of attrition.  Make them a first-class target.
        value = super()._enemy_value(enemy)
        if enemy.cls == HEALER:
            value += 12.0
        if enemy.hp <= self.DMG + 0.01:
            value += 3.0
        return value

    def _defense_moves(self, front, moves) -> None:
        if not front:
            return
        ordered = sorted(front, key=lambda unit: unit.id)
        spawn_count = min(self.HOME_GUARDS, max(1, len(ordered) // 4))
        spawn_slots = self._home_slots("spawn", spawn_count)
        deposit_slots = self._home_slots("deposit", max(1, len(ordered) - spawn_count))

        for index, bot in enumerate(ordered):
            at_spawn = index < spawn_count
            anchor = self.spawn if at_spawn else self.dep
            slots = spawn_slots if at_spawn else deposit_slots
            enemy = self._local_enemy(anchor, self.DEFENSE_RADIUS, bot)
            if enemy is not None:
                moves[bot.id] = self._kite_move(bot, enemy, index, len(ordered))
                continue
            if not slots:
                moves[bot.id] = self._nav(bot.x, bot.y, *anchor)
                continue
            slot_index = index if at_spawn else index - spawn_count
            tx, ty = slots[slot_index % len(slots)]
            if (bot.x - tx) ** 2 + (bot.y - ty) ** 2 > 0.35 ** 2:
                moves[bot.id] = self._nav(bot.x, bot.y, tx, ty)
            else:
                moves[bot.id] = (0.0, 0.0)

    # --------------------------------------------------------------- late assault

    def _payload_slots(self, count: int) -> list:
        key = (round(self.P[0] * 4), round(self.P[1] * 4), count)
        if self._payload_slots_cache and self._payload_slots_cache[0] == key:
            return self._payload_slots_cache[1]
        slots = self._anchor_points(count, [])
        if len(slots) < count:
            slots.extend(self._objective_slots(count - len(slots)))
        self._payload_slots_cache = (key, slots[:count])
        return slots[:count]

    def _assault_moves(self, front, moves) -> None:
        if not front:
            return
        ordered = sorted(front, key=lambda unit: unit.id)
        nearby = [
            enemy for enemy in self.op_fighters
            if (enemy.px - self.P[0]) ** 2 + (enemy.py - self.P[1]) ** 2
            <= (self.ASSAULT_PAYLOAD_RADIUS + 3.0) ** 2
        ]
        push_count = min(self.PUSHERS, max(3, len(ordered) // 4))
        payload_slots = self._payload_slots(push_count)

        # Keep the same healthy vanguard across ticks.  Reassigning by list order
        # makes units swap roles whenever one is healed or killed, which produces a
        # hesitant blob at the exact moment the payload needs bodies.
        alive = {unit.id for unit in ordered}
        self.payload_pusher_ids.intersection_update(alive)
        if len(self.payload_pusher_ids) < min(push_count, len(ordered)):
            candidates = sorted(
                (unit for unit in ordered if unit.id not in self.payload_pusher_ids),
                key=lambda unit: (
                    (unit.x - self.P[0]) ** 2 + (unit.y - self.P[1]) ** 2,
                    -unit.hp,
                    unit.id,
                ),
            )
            self.payload_pusher_ids.update(
                unit.id
                for unit in candidates[: max(0, push_count - len(self.payload_pusher_ids))]
            )

        if self.phase == "prep":
            # The march begins before the endgame, but the mining line and its guards
            # stay in place.  The vanguard takes the shortest route to the circle and
            # the rest follows in a loose ring, giving the old strategy a moving target
            # instead of a single stationary firing line.
            slots = self._objective_slots(len(ordered))
            if not nearby:
                for index, bot in enumerate(ordered):
                    if bot.id in self.payload_pusher_ids and payload_slots:
                        tx, ty = payload_slots[index % len(payload_slots)]
                    else:
                        tx, ty = slots[index % len(slots)] if slots else self.P
                    moves[bot.id] = self._nav(bot.x, bot.y, tx, ty)
                return

        if not nearby:
            # No resistance at the objective: occupy the capture circle quickly.
            slots = self._objective_slots(len(ordered))
            for index, bot in enumerate(ordered):
                tx, ty = slots[index % len(slots)] if slots else self.P
                moves[bot.id] = self._nav(bot.x, bot.y, tx, ty)
            return

        for index, bot in enumerate(ordered):
            target = min(
                nearby,
                key=lambda enemy: (
                    (enemy.px - bot.x) ** 2 + (enemy.py - bot.y) ** 2
                    - (16.0 if enemy.cls == HEALER else 0.0)
                    - (5.0 if enemy.hp <= self.DMG + 0.01 else 0.0)
                ),
            )
            target_distance = math.hypot(target.px - bot.x, target.py - bot.y)
            if bot.id in self.payload_pusher_ids and payload_slots:
                # A pusher has a different job from the firing line: it must enter the
                # capture radius even when an enemy is standing on the opposite edge of
                # the circle.  The old threshold only allowed this while the enemy was
                # far away, so a four-bot defensive ring could freeze the payload forever.
                tx, ty = payload_slots[index % len(payload_slots)]
                in_capture = (bot.x - self.P[0]) ** 2 + (bot.y - self.P[1]) ** 2 <= (
                    self.CAP_R - 0.25
                ) ** 2
                if not in_capture:
                    moves[bot.id] = self._nav(bot.x, bot.y, tx, ty)
                elif (bot.x - tx) ** 2 + (bot.y - ty) ** 2 > 0.55 ** 2:
                    moves[bot.id] = self._nav(bot.x, bot.y, tx, ty)
                else:
                    # Keep the anchor in the circle.  Its turn-and-fire action still
                    # handles the nearby target, while the outer group kites and creates
                    # the movement the anchor cannot afford to make.
                    moves[bot.id] = (0.0, 0.0)
            else:
                moves[bot.id] = self._kite_move(bot, target, index, len(ordered))

    def _battle_moves(self, front, moves) -> None:
        if self.phase == "defense":
            self._defense_moves(front, moves)
        else:
            self._assault_moves(front, moves)

    # --------------------------------------------------------------- support units

    def _extractor_moves(self, extractors, moves) -> dict:
        if self.phase in ("defense", "prep"):
            return super()._extractor_moves(extractors, moves)
        out = {}
        slots = self._payload_slots(len(extractors))
        for index, bot in enumerate(sorted(extractors, key=lambda unit: unit.id)):
            tx, ty = slots[index % len(slots)] if slots else self.P
            moves[bot.id] = self._nav(bot.x, bot.y, tx, ty)
            # Existing extractors cannot change class; use them as objective bodies.
            out[bot.id] = (None, False)
        return out

    def _healer_moves(self, healers, me, moves) -> dict:
        if self.phase == "defense":
            plans = {}
            if not healers:
                return plans
            battles = [unit for unit in me if unit.cls == BATTLE]
            wounded = [unit for unit in battles if unit.hp < self.MAXHP - 0.1]
            ordered = sorted(healers, key=lambda unit: unit.id)
            spawn_healers = max(1, len(ordered) // 4)
            for index, healer in enumerate(ordered):
                anchor = self.spawn if index < spawn_healers else self.dep
                patient = min(
                    wounded,
                    key=lambda unit: (unit.x - healer.x) ** 2 + (unit.y - healer.y) ** 2,
                    default=None,
                )
                if patient is None:
                    slots = self._home_slots("spawn" if index < spawn_healers else "deposit", len(ordered))
                    tx, ty = slots[index % len(slots)] if slots else anchor
                    plans[healer.id] = None
                else:
                    plans[healer.id] = patient.id
                    away_x, away_y = self._away(patient.x, patient.y, self.op_fighters)
                    tx = patient.x + away_x * 1.8
                    ty = patient.y + away_y * 1.8
                moves[healer.id] = self._nav(healer.x, healer.y, tx, ty)
            return plans

        plans = {}
        battles = [unit for unit in me if unit.cls == BATTLE]
        if not healers or not battles:
            return plans

        ordered = sorted(healers, key=lambda unit: unit.id)
        alive_healers = {unit.id for unit in ordered}
        self.home_healer_ids.intersection_update(alive_healers)
        home_count = min(self.HOME_HEALERS, len(ordered))
        if self.phase == "prep" or self.home_threat:
            home_count = min(max(home_count, 1), len(ordered))
        elif self.phase == "assault":
            home_count = min(self.HOME_ASSAULT_GUARDS, len(ordered))
        if len(self.home_healer_ids) < home_count:
            self.home_healer_ids.update(
                healer.id
                for healer in ordered
                if healer.id not in self.home_healer_ids
            )
            self.home_healer_ids = set(list(self.home_healer_ids)[:home_count])

        home_battles = [b for b in battles if b.id in self.home_guard_ids]
        attack_battles = [b for b in battles if b.id not in self.home_guard_ids] or battles
        wounded = [unit for unit in battles if unit.hp < self.MAXHP - 0.1]
        nearby = [
            enemy for enemy in self.op_fighters
            if (enemy.px - self.P[0]) ** 2 + (enemy.py - self.P[1]) ** 2
            <= (self.ASSAULT_PAYLOAD_RADIUS + 3.0) ** 2
        ]

        # Home healers sit behind the guard line and keep healing available locally.
        # They do not follow the payload just because an assault phase started.
        home_index = 0
        for healer in ordered:
            if healer.id not in self.home_healer_ids:
                continue
            candidates = [b for b in wounded if b in home_battles]
            patient = min(
                candidates or home_battles,
                key=lambda unit: (unit.x - healer.x) ** 2 + (unit.y - healer.y) ** 2,
                default=None,
            )
            anchor = self.spawn if home_index == 0 and self.home_anchor == "spawn" else self.dep
            if patient is None:
                slots = self._home_slots("spawn" if anchor == self.spawn else "deposit", max(1, home_count))
                tx, ty = slots[home_index % len(slots)] if slots else anchor
                plans[healer.id] = None
            else:
                plans[healer.id] = patient.id
                away_x, away_y = self._away(patient.x, patient.y, self.home_threat)
                side_x, side_y = -away_y, away_x
                lane = ((home_index % 3) - 1) * 0.7
                tx = patient.x + away_x * 1.8 + side_x * lane
                ty = patient.y + away_y * 1.8 + side_y * lane
            moves[healer.id] = self._nav(healer.x, healer.y, tx, ty)
            home_index += 1

        attack_healers = [h for h in ordered if h.id not in self.home_healer_ids]
        attack_wounded = [unit for unit in wounded if unit.id not in self.home_guard_ids]
        for index, healer in enumerate(attack_healers):
            patient = min(
                attack_wounded,
                key=lambda unit: (unit.x - healer.x) ** 2 + (unit.y - healer.y) ** 2,
                default=None,
            )
            if patient is None:
                patient = attack_battles[index % len(attack_battles)]
            plans[healer.id] = patient.id
            away_x, away_y = self._away(patient.x, patient.y, nearby)
            side_x, side_y = -away_y, away_x
            lane = ((index % 3) - 1) * 0.8
            tx = patient.x + away_x * 1.9 + side_x * lane
            ty = patient.y + away_y * 1.9 + side_y * lane
            moves[healer.id] = self._nav(healer.x, healer.y, tx, ty)
        return plans


class AdvancedStrategyNew(_VerifiedStrategy):
    """V10 fire-control with a payload escort formation.

    V10 is a strong gun controller, but its default movement asks every battle bot to
    chase its nearest enemy and then stop at one distance.  Around the payload that
    creates the exact failure mode this strategy is meant to avoid: the payload sits
    between the army and the enemy, the aim planner correctly refuses the blocked
    shots, and the whole front becomes a stationary line.

    This subclass keeps V10's production, healing and exact shot replay.  It changes
    only the battle movement: a small vanguard stays on the friendly side of the
    payload and contests it under cover, while two support lanes take clear shots from
    the sides.  In open ground the same units use a staggered radial kite instead of
    walking into one clump.  The formation is deliberately computed from the current
    payload and enemy centroid, so it follows corners of the payload path rather than
    assuming that the first straight segment is always active.
    """

    ASSAULT_PREP_TICKS = _env("IAMABOT_NEW_ASSAULT_PREP", 1200)

    # Payload formation.  The first group stays inside capture range, but outside the
    # solid hull.  The other groups are far enough out to shoot around the payload.
    PUSHER_COUNT = _env("IAMABOT_NEW_PUSHER_COUNT", 4)
    PUSHER_RADIUS = _env("IAMABOT_NEW_PUSHER_RADIUS", 1.65)
    PUSHER_LANE = _env("IAMABOT_NEW_PUSHER_LANE", 0.62)
    SUPPORT_RADIUS = _env("IAMABOT_NEW_SUPPORT_RADIUS", 7.8)
    SUPPORT_LANE = _env("IAMABOT_NEW_SUPPORT_LANE", 3.0)
    SUPPORT_FORWARD = _env("IAMABOT_NEW_SUPPORT_FORWARD", 1.0)
    PAYLOAD_FIGHT_RADIUS = _env("IAMABOT_NEW_PAYLOAD_FIGHT_RADIUS", 13.0)
    PAYLOAD_APPROACH_RADIUS = _env("IAMABOT_NEW_PAYLOAD_APPROACH_RADIUS", 16.0)
    FORMATION_REFRESH = _env("IAMABOT_NEW_FORMATION_REFRESH", 18)
    FORMATION_MIN_TICK = _env("IAMABOT_NEW_FORMATION_MIN_TICK", 1500)
    FORMATION_MIN_BATTLE = _env("IAMABOT_NEW_FORMATION_MIN_BATTLE", 10)
    FORMATION_MIN_POWER = _env("IAMABOT_NEW_FORMATION_MIN_POWER", 0.88)
    FORMATION_MIN_LOCAL_POWER = _env("IAMABOT_NEW_FORMATION_MIN_LOCAL_POWER", 0.78)

    # Open-field pullback band.  These values leave a useful firing distance while
    # making the desired point alternate between the near and far edge of the band.
    OPEN_NEAR = _env("IAMABOT_NEW_OPEN_NEAR", 7.0)
    OPEN_FAR = _env("IAMABOT_NEW_OPEN_FAR", 9.0)
    OPEN_PERIOD = _env("IAMABOT_NEW_OPEN_PERIOD", 42)
    OPEN_LANE = _env("IAMABOT_NEW_OPEN_LANE", 0.7)

    _ROLE_PUSH = 0
    _ROLE_LEFT = 1
    _ROLE_RIGHT = 2
    _ROLE_REAR = 3

    def __init__(self) -> None:
        super().__init__()
        self.phase = "defense"
        self._new_roles: dict[int, int] = {}
        self._new_role_tick = -999
        self._new_targets: dict[int, tuple] = {}
        self._new_target_key = None
        self._new_pusher_ids: set[int] = set()

    def _set_phase(self, tick: int) -> None:
        assault_tick = max(0, self.END_T - self.ASSAULT_PREP_TICKS)
        if tick < assault_tick:
            self.phase = "defense"
        elif tick < self.END_T:
            self.phase = "prep"
        else:
            self.phase = "assault"

    # ------------------------------------------------------------ payload tactics

    @staticmethod
    def _unit_vec(x: float, y: float, fallback=(1.0, 0.0)):
        n = math.hypot(x, y)
        if n < 1e-6:
            return fallback
        return x / n, y / n

    def _new_enemy_front(self, fighters):
        """Return the enemy centroid and the direction from payload to that centroid."""
        px, py = self.P
        if not fighters:
            return px + self.u[0] * 8.0, py + self.u[1] * 8.0, self.u
        # Bodies close to the objective determine the front.  If the enemy is still
        # marching in, use all fighters so the escort does not choose a stale side.
        near = [
            e
            for e in fighters
            if (e.px - px) ** 2 + (e.py - py) ** 2 <= self.PAYLOAD_APPROACH_RADIUS ** 2
        ]
        pool = near or fighters
        ex = sum(e.px for e in pool) / len(pool)
        ey = sum(e.py for e in pool) / len(pool)
        ux, uy = self._unit_vec(ex - px, ey - py, self.u)
        return ex, ey, (ux, uy)

    def _new_role_assignment(self, front):
        """Keep roles sticky, but replace dead units and resize the pusher group."""
        alive = {b.id for b in front}
        self._new_roles = {bid: role for bid, role in self._new_roles.items() if bid in alive}
        self._new_pusher_ids.intersection_update(alive)
        if not front:
            return

        wanted_pushers = min(
            len(front),
            max(2, min(self.PUSHER_COUNT, max(2, len(front) // 4))),
        )
        # A pusher is selected by distance to the payload, then health.  This keeps the
        # closest durable bodies in the cover group instead of sending a wounded bot into
        # the capture circle just because its id is small.
        if len(self._new_pusher_ids) < wanted_pushers:
            candidates = sorted(
                (b for b in front if b.id not in self._new_pusher_ids),
                key=lambda b: (
                    (b.x - self.P[0]) ** 2 + (b.y - self.P[1]) ** 2,
                    -b.hp,
                    b.id,
                ),
            )
            self._new_pusher_ids.update(
                b.id for b in candidates[: wanted_pushers - len(self._new_pusher_ids)]
            )
        elif len(self._new_pusher_ids) > wanted_pushers:
            keep = sorted(
                (b for b in front if b.id in self._new_pusher_ids),
                key=lambda b: (
                    (b.x - self.P[0]) ** 2 + (b.y - self.P[1]) ** 2,
                    -b.hp,
                    b.id,
                ),
            )[:wanted_pushers]
            self._new_pusher_ids = {b.id for b in keep}

        # Keep side assignments stable.  New support units are divided by their current
        # lateral position, with id as a deterministic tie-breaker.
        support = [b for b in front if b.id not in self._new_pusher_ids]
        for b in front:
            if b.id in self._new_pusher_ids:
                self._new_roles[b.id] = self._ROLE_PUSH
        missing = [b for b in support if b.id not in self._new_roles]
        for b in missing:
            self._new_roles[b.id] = self._ROLE_LEFT if b.id % 2 else self._ROLE_RIGHT
        self._new_role_tick = self.T

    def _new_solid_detour(self, fx, fy, tx, ty):
        """Navigate to a target while going around the payload when it is in the way.

        `navigate_to` knows about walls, not the moving payload.  A direct request to a
        point on the far side therefore pushes a bot into the payload forever.  The
        waypoint is only used while the segment intersects the payload hull; after the
        bot reaches the side, normal navigation takes over.
        """
        px, py = self.P
        clear = self.P_R + self.R + 0.22
        if _seg_dist2(px, py, fx, fy, tx, ty) < clear * clear:
            dx, dy = tx - fx, ty - fy
            n = math.hypot(dx, dy)
            if n > 1e-6:
                nx, ny = -dy / n, dx / n
                side = (px - fx) * nx + (py - fy) * ny
                sign = -1.0 if side > 0.0 else 1.0
                for extra in (0.0, 0.7, -0.7):
                    wx = px + nx * sign * (clear + extra)
                    wy = py + ny * sign * (clear + extra)
                    if self._disc_free(wx, wy, self.R + 0.06):
                        return self._nav(fx, fy, wx, wy)
        return self._nav(fx, fy, tx, ty)

    def _new_point(self, x, y, avoid=None):
        """Return a nearby legal grid point, without doing a full search every tick."""
        if self._disc_free(x, y, self.R + 0.06):
            return x, y
        best = None
        best_d = 1e9
        avoid = avoid or []
        for gx, gy in self.coarse:
            d = (gx - x) ** 2 + (gy - y) ** 2
            if d >= best_d or d > 3.0 ** 2:
                continue
            if any((gx - ax) ** 2 + (gy - ay) ** 2 < self.SPACING ** 2 for ax, ay in avoid):
                continue
            best = (gx, gy)
            best_d = d
        return best or (x, y)

    def _new_payload_targets(self, front, fighters):
        """Build pusher and two side-lane destinations around the current payload."""
        if not front:
            return {}
        ex, ey, (ux, uy) = self._new_enemy_front(fighters)
        px, py = self.P
        sx, sy = -uy, ux
        key = (
            self.phase,
            round(px * 2.0),
            round(py * 2.0),
            round(ex / 1.5),
            round(ey / 1.5),
            len(front),
        )
        if (
            self._new_target_key == key
            and self.T - self._new_role_tick < self.FORMATION_REFRESH
            and len(self._new_targets) >= len(front)
        ):
            return self._new_targets

        self._new_role_assignment(front)
        pushers = [b for b in front if self._new_roles.get(b.id) == self._ROLE_PUSH]
        left = [b for b in front if self._new_roles.get(b.id) == self._ROLE_LEFT]
        right = [b for b in front if self._new_roles.get(b.id) == self._ROLE_RIGHT]
        rear = [b for b in front if self._new_roles.get(b.id) == self._ROLE_REAR]
        targets = {}

        # Pusher destinations are a shallow arc on our side of the payload.  Every
        # destination is within capture range, so one uncontested pusher can move the
        # objective while the support lanes keep shooting.
        n = max(1, len(pushers))
        for index, b in enumerate(sorted(pushers, key=lambda u: u.id)):
            lateral = (index - (n - 1) / 2.0) * self.PUSHER_LANE
            tx = px - ux * self.PUSHER_RADIUS + sx * lateral
            ty = py - uy * self.PUSHER_RADIUS + sy * lateral
            targets[b.id] = self._new_point(tx, ty)

        # Side support positions are built from the enemy front, not from the bot's
        # current position.  This creates a crossfire: the left lane can see targets
        # that are hidden from the right lane and vice versa.
        def support_targets(group, side):
            count = max(1, len(group))
            for index, b in enumerate(sorted(group, key=lambda u: u.id)):
                lane = (index - (count - 1) / 2.0) * 1.05
                # Stay on the support side of the payload and behind the front line,
                # with the payload offset enough to avoid its centreline shadow.
                side_offset = side * (self.SUPPORT_LANE + lane)
                tx = ex - ux * self.SUPPORT_RADIUS + sx * side_offset
                ty = ey - uy * self.SUPPORT_RADIUS + sy * side_offset
                candidates = [(tx, ty)]
                # If the first side is blocked by a wall or still lies in payload
                # shadow, try a slightly more lateral and slightly rear point.
                for lateral in (side * 1.4, side * 2.3, -side * 1.4, -side * 2.3):
                    candidates.append(
                        (
                            ex - ux * (self.SUPPORT_RADIUS + self.SUPPORT_FORWARD) + sx * (side_offset + lateral),
                            ey - uy * (self.SUPPORT_RADIUS + self.SUPPORT_FORWARD) + sy * (side_offset + lateral),
                        )
                    )
                chosen = None
                for cx, cy in candidates:
                    cx, cy = self._new_point(cx, cy)
                    if not self._payload_blocks(cx, cy, ex, ey) and self._los(cx, cy, ex, ey):
                        chosen = (cx, cy)
                        break
                targets[b.id] = chosen or self._new_point(tx, ty)

        support_targets(left, -1.0)
        support_targets(right, 1.0)
        # Remaining bots form a rear fire/support line.  They stay on our side of the
        # payload, but outside capture range, and can rotate to either flank naturally.
        for index, b in enumerate(sorted(rear, key=lambda u: u.id)):
            lateral = (index - (max(1, len(rear)) - 1) / 2.0) * 1.1
            tx = px - ux * 4.4 + sx * lateral
            ty = py - uy * 4.4 + sy * lateral
            targets[b.id] = self._new_point(tx, ty)
        self._new_targets = targets
        self._new_target_key = key
        return targets

    def _new_open_move(self, b, enemy, index, count, moves):
        """Staggered forward/backward pull in open ground."""
        dx, dy = b.x - enemy.px, b.y - enemy.py
        d = math.hypot(dx, dy)
        if d < 1e-5:
            dx, dy = (1.0, 0.0) if b.id % 2 else (-1.0, 0.0)
            d = 1.0
        ux, uy = dx / d, dy / d
        sx, sy = -uy, ux
        lane = ((index % 5) - 2) * self.OPEN_LANE
        phase = ((self.T // self.OPEN_PERIOD) + b.id) & 1
        wanted = self.OPEN_NEAR if phase == 0 else self.OPEN_FAR
        if d < self.OPEN_NEAR - 0.35:
            tx, ty = b.x + ux * 2.0, b.y + uy * 2.0
        elif d > self.OPEN_FAR + 0.45:
            tx, ty = enemy.px + ux * self.OPEN_FAR + sx * lane, enemy.py + uy * self.OPEN_FAR + sy * lane
        else:
            tx, ty = enemy.px + ux * wanted + sx * lane, enemy.py + uy * wanted + sy * lane
        moves[b.id] = self._nav(b.x, b.y, tx, ty)

    def _battle_moves(self, front, moves) -> None:
        if not front:
            return

        fighters = list(self.op_fighters)
        if self.home:
            # V10's home-raid response is already conservative and important; do not
            # pull the escort away from a point-blank deposit attack.
            return super()._battle_moves(front, moves)

        px, py = self.P
        near_payload = any(
            (b.x - px) ** 2 + (b.y - py) ** 2 <= self.PAYLOAD_FIGHT_RADIUS ** 2
            for b in front
        )
        enemy_near = any(
            (e.px - px) ** 2 + (e.py - py) ** 2 <= self.PAYLOAD_FIGHT_RADIUS ** 2
            for e in fighters
        )

        # Do not reveal the formation while it is still a handful of reinforcements.
        # The early fight is where V10's close-range focus fire is strongest; the new
        # formation becomes useful after enough bodies exist to cover both side lanes.
        # If we are already locally outnumbered, its retreat/press decision is also a
        # better emergency response than sending a small escort into the circle.
        if (
            self.T < self.FORMATION_MIN_TICK
            or len(front) < self.FORMATION_MIN_BATTLE
            or self.my_all < self.FORMATION_MIN_POWER * max(self.op_all, 1.0)
            or (
                self.op_near > 0.5
                and self.my_near < self.FORMATION_MIN_LOCAL_POWER * self.op_near
            )
        ):
            return super()._battle_moves(front, moves)

        # No local fight yet: march to the payload as a group.  Once an enemy is near,
        # the formation below takes over.  This prevents a wall-side chase from dragging
        # the whole army away from the objective.
        if not fighters:
            targets = self._new_payload_targets(front, [])
            for b in front:
                tx, ty = targets.get(b.id, (px, py))
                moves[b.id] = self._new_solid_detour(b.x, b.y, tx, ty)
            return

        if not near_payload and not enemy_near:
            # Open-field fight: keep the army mobile and spread across the front.  The
            # nearest enemy is only used to define each bot's local kite target.
            for index, b in enumerate(sorted(front, key=lambda u: u.id)):
                enemy = min(
                    fighters,
                    key=lambda e: (e.px - b.x) ** 2 + (e.py - b.y) ** 2,
                )
                if b.hp <= self.RETREAT_HP and (
                    (enemy.px - b.x) ** 2 + (enemy.py - b.y) ** 2 <= (self.RANGE + 2.0) ** 2
                ):
                    ax, ay = self._away(b.x, b.y, fighters)
                    moves[b.id] = self._nav(b.x, b.y, b.x + ax * 2.5, b.y + ay * 2.5)
                else:
                    self._new_open_move(b, enemy, index, len(front), moves)
            return

        # Payload fight: assign the close cover group and the two firing lanes.
        targets = self._new_payload_targets(front, fighters)
        for b in front:
            role = self._new_roles.get(b.id, self._ROLE_RIGHT)
            tx, ty = targets.get(b.id, (px, py))
            if b.hp <= self.RETREAT_HP:
                enemy = min(fighters, key=lambda e: (e.px - b.x) ** 2 + (e.py - b.y) ** 2)
                d2 = (enemy.px - b.x) ** 2 + (enemy.py - b.y) ** 2
                if d2 <= (self.RANGE + 2.0) ** 2:
                    ax, ay = self._away(b.x, b.y, fighters)
                    moves[b.id] = self._nav(b.x, b.y, b.x + ax * 2.5, b.y + ay * 2.5)
                    continue
            if role == self._ROLE_PUSH:
                moves[b.id] = self._new_solid_detour(b.x, b.y, tx, ty)
            else:
                # Support/rear units may move laterally even after reaching their point;
                # this small periodic offset prevents a stationary firing wall without
                # pulling them through the payload shadow.
                moves[b.id] = self._new_solid_detour(b.x, b.y, tx, ty)

    def _tick(self, state: GameState) -> FleetAction:
        # V10 retains the proven production, healing and shot replay via dynamic dispatch.
        self._set_phase(state.tick)
        return super()._tick(state)


class ReferenceStrategy:
    """Deliberately simple active baseline used only by local A/B tests."""

    def __call__(self, state: GameState) -> FleetAction:
        conf = get_config()
        action = FleetAction.new()
        allies = list(state.fleet_me)
        enemies = list(state.fleet_other)
        n_ext = sum(1 for b in allies if b.special.tag == EXTRACTOR)
        action.fabricator_next = EXTRACTOR if n_ext < 3 else BATTLE
        action.rush_order = (
            state.tick < conf.max_ticks - conf.endgame_ticks
            and not state.fleet_me.is_full()
            and state.fabricator_me.tokens >= conf.fabricator.rush_cost
        )
        mine = state.deposit_me.pos + Vec2(0.0, conf.deposit.radius + conf.bot.radius + 0.2)
        payload = state.payload_pos()
        for bot in allies:
            bot_action = action.bots[bot.id]
            if bot.special.tag == EXTRACTOR:
                bot_action.move_action = move_bot(navigate_to(bot.pos, mine))
                bot_action.turn_action = turn_towards(state.deposit_me.pos)
                bot_action.special_action = SpecialAction.Extractor(mine=True)
                continue
            enemy = min(enemies, key=lambda other: bot.pos.dist_sq(other.pos), default=None)
            target = enemy.pos if enemy else payload
            bot_action.move_action = move_bot(navigate_to(bot.pos, payload))
            bot_action.turn_action = turn_towards(target)
            can_fire = (
                enemy is not None
                and bot.next_fire_tick <= state.tick
                and bot.pos.dist(target) <= conf.bot.blaster_range
                and line_of_sight(bot.pos, target)
            )
            bot_action.special_action = SpecialAction.Battle(fire=can_fire)
        return action

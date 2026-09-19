"""IAMABOT tournament controller (v6.1 + Codex champion overlay).

One file on purpose: `AdvancedStrategy` is the v6.1 controller; `ChampionStrategy`
subclasses it with the evidence-driven opening, payload focus, wall-splash fire
control and healer aiming. Submit `ChampionStrategy`.
"""

from __future__ import annotations

import math
import os

from . import *

try:  # raw entry points: same functions as core.channel, minus the Vec2 wrapping
    import ctypes as _ctypes

    from core import channel as _channel_mod
    from core._generated import bindings as _raw

    _RAW_OK = True
except Exception:  # pragma: no cover - defensive, the starterpack always has these
    _RAW_OK = False


DEBUG = bool(os.environ.get("IAMABOT_DEBUG"))

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
    # them by mid game), nine battle bots and five healers win the first fight.  A third
    # of the fighting force stays healers: sustain beat raw guns in every head-to-head.
    OPENING = _env("IAMABOT_OPENING", "EBBEHBHBEBHBBHBBB")
    EXTRACTOR_TARGET = _env("IAMABOT_EXTRACTORS", 6)
    HEALER_RATIO = _env("IAMABOT_HEALER_RATIO", 0.33)
    EXTRACTOR_CUTOFF = 4300  # an extractor built later cannot pay for itself

    # Engagement distances (to the nearest enemy fighter) per stance.
    D_ATTACK = _env("IAMABOT_D_ATTACK", 6.3)
    D_HOLD = _env("IAMABOT_D_HOLD", 7.4)
    ATTACK_RATIO = _env("IAMABOT_ATTACK_RATIO", 1.25)
    RETREAT_RATIO = _env("IAMABOT_RETREAT_RATIO", 0.72)

    MOVE_MODE = _env("IAMABOT_MOVE", "press")
    D_PRESS = _env("IAMABOT_D_PRESS", 6.0)
    PRESS_LOS = _env("IAMABOT_PRESS_LOS", 0)
    STALL_TICKS = _env("IAMABOT_STALL", 250)

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
        self.debug_next = 0
        self.why: dict = {}

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
            return self._tick(state)
        except Exception:  # never let a bug take the whole fleet out of the match
            if DEBUG:
                import traceback

                traceback.print_exc()
            try:
                return self._fallback(state)
            except Exception:
                return FleetAction.new()

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
        guards = self._pick_guards(battles, op)
        self.n_guards = len(guards)
        front = [b for b in battles if b.id not in guards]
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
                f"zone={len(self.me_in_zone)}v{len(self.op_in_zone)} guards={self.n_guards} "
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
        `D_PRESS` with a clear line (no wall, not behind the payload), then stands and
        shoots.  All guns engage at once and converge on the enemy's nearest bodies, which
        beat every formation we tried head to head.  With no enemy fighter anywhere the
        army takes the capture circle and pushes."""
        if not front:
            return
        fighters = self.op_fighters
        if self.MOVE_MODE == "slots" or not fighters:
            return self._slot_moves(front, moves)
        D = self.D_PRESS
        D2 = D * D
        shoot2 = (self.RANGE - 0.6) ** 2
        # Tiebreak insurance: armies parked on either side of a wall never shoot, and at
        # max_ticks the payload's side decides.  If it sits on our half, unmoved, with no
        # enemy in the circle, two bots walk in and push it back; ahead, nothing changes.
        if (
            self.capture <= 0.02
            and self.T - self.capture_moved > self.STALL_TICKS
            and self.T > 1500
            and not self.op_in_zone
        ):
            front = self._anchor_duty(front, moves)
        for b in front:
            ranked = sorted(fighters, key=lambda o: (o.px - b.x) ** 2 + (o.py - b.y) ** 2)
            e = ranked[0]
            d2 = (e.px - b.x) ** 2 + (e.py - b.y) ** 2
            if d2 > D2:
                moves[b.id] = self._nav(b.x, b.y, e.px, e.py)
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
            moves[b.id] = (0.0, 0.0)

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
            moves[h.id] = self._nav(h.x, h.y, tx, ty)
        return plans

    def _heal_triggers(self, healers, plans, moves) -> dict:
        """Final heading and trigger per healer, with the post-separation move."""
        out = {}
        ax, ay = self.AC
        by_id = {u.id: u for u in self.me}
        for h in healers:
            mx, my = moves.get(h.id, (0.0, 0.0))
            ox = h.x + mx * self.SPEED
            oy = h.y + my * self.SPEED
            aid = plans.get(h.id)
            a = by_id.get(aid) if aid is not None else None
            if a is None:
                out[h.id] = (0, _ang(ax - ox, ay - oy), False)
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
        act = FleetAction.new()
        conf = get_config()
        act.fabricator_next = BATTLE
        act.rush_order = (
            state.tick < conf.max_ticks - conf.endgame_ticks
            and not state.fleet_me.is_full()
            and state.fabricator_me.tokens >= conf.fabricator.rush_cost
        )
        payload = state.payload_pos()
        enemies = list(state.fleet_other)
        for bot in state.fleet_me:
            ba = act.bots[bot.id]
            tag = bot.special.tag
            ba.move_action = move_bot(navigate_to(bot.pos, payload))
            if tag == EXTRACTOR:
                ba.turn_action = turn_towards(state.deposit_me.pos)
                ba.special_action = SpecialAction.Extractor(mine=True)
                ba.move_action = move_bot(navigate_to(bot.pos, state.deposit_me.pos))
            elif tag == HEALER:
                ba.special_action = SpecialAction.Healer(fire=False, target=0)
            else:
                enemy = min(enemies, key=lambda o: bot.pos.dist_sq(o.pos), default=None)
                if enemy is not None:
                    ba.turn_action = turn_towards(enemy.pos)
                ba.special_action = SpecialAction.Battle(fire=False)
        return act


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


class ChampionStrategy(AdvancedStrategy):
    # Tested opening: 3 extractors, 7 battle bots, 7 healers, then guns catch up.
    OPENING = _env("CHAMPION_OPENING", "EBHBHBHEBHBHEBHBH")
    HEALER_RATIO = _env("CHAMPION_HEALER_RATIO", 0.45)
    EXTRACTOR_TARGET = _env("CHAMPION_EXTRACTORS", 3)
    D_PRESS = _env("CHAMPION_DISTANCE", 6.0)
    WALL_SPLASH = _env("CHAMPION_WALL_SPLASH", 1)
    HEAL_FIX = _env("CHAMPION_HEAL_FIX", 1)
    MARCH = _env("CHAMPION_MARCH", 1)
    SPREAD = _env("CHAMPION_SPREAD", 0.85)
    ADAPTIVE_SPREAD = _env("CHAMPION_ADAPTIVE_SPREAD", 1)
    OBJECTIVE = _env("CHAMPION_OBJECTIVE", 1)
    EARLY_ECONOMY = _env("CHAMPION_EARLY_ECONOMY", 0)
    GUARDS = _env("CHAMPION_GUARDS", 0)
    GUN_RESPONSE = _env("CHAMPION_GUN_RESPONSE", 1)

    def _setup(self, state):
        super()._setup(state)
        self.walls = {(x, y) for x in range(MAP_SIZE) for y in range(MAP_SIZE)
                      if int(self.conf.map[x][y]) == int(MapTile.Wall)}
        self.miner_invasion = False
        self.gun_heavy_opening = False

    def _assess(self, battles, healers, op_fighters):
        super()._assess(battles, healers, op_fighters)
        # Identify a mass mining incursion from positions, never from a team name.
        invaders = sum(e.cls == EXTRACTOR and
                       (e.x-self.dep[0])**2+(e.y-self.dep[1])**2 < 20.0**2
                       for e in self.op)
        if self.T < 1800 and invaders >= 3:
            self.miner_invasion = True
        if 20 <= self.T <= 60:
            nb = sum(e.cls == BATTLE for e in self.op)
            nh = sum(e.cls == HEALER for e in self.op)
            self.gun_heavy_opening = nb >= 10 and nh <= 2

    def _next_class(self, counts, tick):
        old = self.EXTRACTOR_TARGET
        old_healing = self.HEALER_RATIO
        if self.GUN_RESPONSE and self.gun_heavy_opening:
            self.HEALER_RATIO = max(old_healing, .48)
        if not self.EARLY_ECONOMY and tick < 1100:
            self.EXTRACTOR_TARGET = min(old, 3)
        try:
            return super()._next_class(counts, tick)
        finally:
            self.EXTRACTOR_TARGET = old
            self.HEALER_RATIO = old_healing

    def _pick_guards(self, battles, op):
        if self.GUARDS:
            return super()._pick_guards(battles, op)
        # Three miners are expendable when defending them would surrender the objective.
        self.raid = [e for e in op if e.cls == BATTLE
                     and (e.x-self.dep[0])**2+(e.y-self.dep[1])**2 < 121]
        return {}

    def _battle_moves(self, front, moves):
        if not self.OBJECTIVE:
            return super()._battle_moves(front, moves)
        fighters = self.op_fighters
        relevant = [e for e in fighters
                    if (e.x-self.P[0])**2+(e.y-self.P[1])**2 <= 12.0**2]
        if not relevant:
            # An enemy deposit raid must not lure every gun away from a free payload.
            slots = self._objective_slots(len(front)) if front else []
            for b, (tx, ty) in zip(sorted(front, key=lambda b: (b.x-self.P[0])**2+(b.y-self.P[1])**2), slots):
                moves[b.id] = self._nav(b.x,b.y,tx,ty)
            return
        self.op_fighters = relevant
        try:
            return super()._battle_moves(front, moves)
        finally:
            self.op_fighters = fighters

    def _separate(self, me, moves):
        old = self.SPACING
        # Don't spread the march column before contact and stagger its arrival by hundreds of ticks.
        self.SPACING = self.SPREAD
        if self.ADAPTIVE_SPREAD and self.miner_invasion:
            self.SPACING = max(self.SPACING, 1.02)
        if self.MARCH and self.T < 420:
            self.SPACING = min(self.SPREAD, .6)
        try:
            return super()._separate(me, moves)
        finally:
            self.SPACING = old

    def _wall_distance(self, ox, oy, dx, dy, limit):
        """Grid DDA: first wall on the same zero-radius ray as the referee."""
        x, y = math.floor(ox), math.floor(oy)
        if (x, y) in self.walls:
            return 0.0
        sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
        tx = ((x + 1 - ox) / dx if dx > 0 else (x - ox) / dx) if abs(dx) > 1e-12 else math.inf
        ty = ((y + 1 - oy) / dy if dy > 0 else (y - oy) / dy) if abs(dy) > 1e-12 else math.inf
        ix = abs(1 / dx) if abs(dx) > 1e-12 else math.inf
        iy = abs(1 / dy) if abs(dy) > 1e-12 else math.inf
        for _ in range(2 * MAP_SIZE + 4):
            if tx < ty:
                t, x, tx = tx, x + sx, tx + ix
            else:
                t, y, ty = ty, y + sy, ty + iy
            if t > limit:
                return limit
            if (x, y) in self.walls or not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE):
                return max(0.0, t)
        return limit

    def _shot_victims(self, ox, oy, heading, op, claimed):
        if not self.WALL_SPLASH:
            return super()._shot_victims(ox, oy, heading, op, claimed)
        dx, dy = math.cos(heading * RAD), math.sin(heading * RAD)
        stop = min(self.RANGE, _ray_boundary(ox, oy, dx, dy, self.SIZE))
        stop = self._wall_distance(ox, oy, dx, dy, stop)
        for cx, cy, radius in ((self.P[0], self.P[1], self.P_R),
                               (*self.dep, self.DEP_R), (*self.dep_other, self.DEP_R)):
            t = _ray_circle(ox, oy, dx, dy, cx, cy, radius)
            if t is not None:
                stop = min(stop, t)
        result = None
        # Unknown opponent movement remains a prediction, not an exact simulation.
        for predicted in (True, False):
            impact = stop
            for e in op:
                ex, ey = (e.px, e.py) if predicted else (e.x, e.y)
                t = _ray_circle(ox, oy, dx, dy, ex, ey, self.R)
                if t is not None:
                    impact = min(impact, t)
            ix, iy = ox + dx * impact, oy + dy * impact
            victims = set()
            for e in op:
                if e.inv > self.T or e.id in claimed:
                    continue
                ex, ey = (e.px, e.py) if predicted else (e.x, e.y)
                if (ex - ix) ** 2 + (ey - iy) ** 2 <= self.SPLASH ** 2 - 0.004:
                    victims.add(e.id)
            result = victims if result is None else result & victims
            if not result:
                return None
        return result

    def _heal_triggers(self, healers, plans, moves):
        if not self.HEAL_FIX:
            return super()._heal_triggers(healers, plans, moves)
        out, used = {}, {}
        allies = {u.id: u for u in self.me}
        for h in healers:
            mx, my = moves.get(h.id, (0.0, 0.0))
            ox, oy = h.x + mx * self.SPEED, h.y + my * self.SPEED
            preferred = plans.get(h.id)
            feasible = []
            for a in self.me:
                if a.id == h.id or a.hp >= self.MAXHP - 0.02 or used.get(a.id, 0) >= self.STACK:
                    continue
                # We know our own final movement command; last tick's velocity is stale.
                amx, amy = moves.get(a.id, (0.0, 0.0))
                ex, ey = a.x + amx * self.SPEED, a.y + amy * self.SPEED
                distance = math.hypot(ex - ox, ey - oy)
                if distance > self.HEAL_R - 0.03:
                    continue
                want = _ang(ex - ox, ey - oy)
                after = h.ang + max(-self.TURN, min(self.TURN, _adiff(want, h.ang)))
                if abs(_adiff(want, after)) > self.HEAL_HALF - 1.0 or not self._los(ox, oy, ex, ey):
                    continue
                need = self.MAXHP - a.hp - used.get(a.id, 0) * self.conf.bot.heal_per_tick
                if need <= 0.01:
                    continue
                value = min(3.0, need) + (2.0 if a.id == preferred else 0.0)
                value += 1.5 if a.cls == BATTLE else 0.5
                if a.hp < self.DMG + 0.2:
                    value += 2.0
                feasible.append((value, a.id, want))
            if feasible:
                _, aid, want = max(feasible)
                used[aid] = used.get(aid, 0) + 1
                out[h.id] = (aid, want, True)
                self.heal_target[h.id] = aid
            else:
                a = allies.get(preferred)
                tx, ty = (a.x, a.y) if a is not None else self.AC
                out[h.id] = (a.id if a is not None else 0, _ang(tx - ox, ty - oy), False)
        return out

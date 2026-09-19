"""MechMania 32 bot -- an economy-first tug-of-war strategy.

Everything below is read from `get_config()` at runtime, but for orientation these are
the numbers this season ships with:

    bot      10 hp, speed 0.05/tick, turn 3 deg/tick, radius 0.25
    blaster  3 damage, range 10, cooldown 60 ticks, splash 0.3, 15 ticks of
             invulnerability on the bot that was hit
    healer   0.05 hp/tick, range 3, 90 degree arc, at most 3 healers stacked
    mining   0.05 tokens/tick per slot, 8 slots per deposit SHARED by both teams
    economy  rush order 50 tokens, free bot every 200 ticks, 800 tokens at tick 0
    match    9000 ticks, the last 3000 are the endgame (no building at all)
    payload  capture radius 2.5, 2400 uncontested ticks from centre to a goal

Four facts drive every decision in this file:

1.  PRESENCE, NOT NUMBERS, MOVES THE PAYLOAD. The engine freezes it the moment both
    fleets have at least one bot inside the capture radius. Denying a push costs one
    body; completing one costs the whole zone. So we never let the zone go empty, and
    we do not need to win a fight to stop a push.

2.  A SHOT COSTS 60 TICKS WHETHER OR NOT IT LANDS. The ray is hitscan along the bot's
    facing after this tick's turn, and it only connects if it passes within a hull
    radius of the target. A bot that was hit is invulnerable for 15 ticks, so a second
    shot into it the same tick is thrown away. Fire discipline -- ready, in range,
    aimed, nothing in the way, target vulnerable, no ally already shooting it -- is
    worth more than extra guns.

3.  AN EXTRACTOR PAYS FOR ITSELF IN 1000 TICKS and prints roughly six more bots over
    the build window, so early economy compounds. We open heavy on extractors, deny
    the payload while we are out-gunned, and press once production has flipped the
    numbers. Tokens do nothing in the bank (third tiebreaker), so they are spent the
    tick we can afford them.

4.  SPLASH MEASURES TO THE CENTRE (splash + bot radius = 0.55 here), so two bots
    closer than ~1.1 to each other die to the same blast. Every formation position in
    this file is spaced further apart than that.

Two consequences of those that are easy to miss, and that decide matches:

*   THE PAYLOAD IS SOLID AND STOPS BLASTER RAYS. A fleet that forms up behind it cannot
    shoot the bots camping on the far side, which is how 32 bots fail to clear a zone
    held by one. Firing posts are scored on whether they can actually put a ray into
    where the enemy is standing, and the holders ring the payload rather than queue
    behind it.
*   A FLEET IS CAPPED AT 32. Once both sides are capped, an extractor is a body that
    cannot shoot, so before the fabricator shuts off the economy is cashed in: surplus
    extractors self-destruct and the banked tokens rush battle bots into their slots.

Tuning knobs are all in the block at the top.
"""

from . import *

import math

# =====================================================================================
# Tuning knobs
# =====================================================================================

# --- economy -------------------------------------------------------------------------
OPENING_EXTRACTORS = 7        # extractors bought before the opening 800 tokens go to guns
STEADY_EXTRACTORS = 8         # ceiling once the economy is running (deposit cap is 8)
EXTRACTOR_PAYBACK_TICKS = 1200  # an extractor built later than this before the fabricator
                                # shuts off can never earn back its own rush cost
CASHOUT_TICKS = 900           # ...and once the fleet is capped and the build window is
                              # nearly over, a mining slot is worth less than a gun: trade
                              # the extractors in for battle bots while there is still
                              # time for them to walk to the front

# --- fleet composition ---------------------------------------------------------------
MAX_HEALERS = 4
HEALER_PER_BATTLE = 6         # one healer per this many battle bots
HEALER_UNLOCK = 8             # ...and none at all until the army is this big

# --- formation -------------------------------------------------------------------------
HOLD_RINGS = (1.85, 2.3)      # in-zone rings (collision floor 1.0, capture radius 2.5)
# Where the in-zone bodies stand, in degrees off the direction the enemy arrives from.
# The payload is solid and stops blaster rays, so a fleet that parks entirely *behind* it
# cannot shoot the bots camping on the other side -- which is exactly how a fleet of 32
# fails to clear a zone held by one. The order below spreads the holders around it: one
# in front where the shots land, one behind, then the flanks.
HOLD_OFFSETS = (0.0, -60.0, 60.0, 180.0, -120.0, 120.0, -30.0, 30.0, -150.0, 150.0)
HOLD_WHEN_PRESSING = 8        # bodies parked in the zone when we have the upper hand
HOLD_WHEN_OUTGUNNED = 2       # ...and when we do not: just enough to freeze the payload
# The firing line is a lattice, not a ring: the payload track runs down corridors barely
# three tiles wide, so most of any ring around the payload is solid wall, while a lattice
# fills whatever shape the corridor actually is. A step wider than the splash diameter
# keeps the fleet un-stacked for free.
SCREEN_STEP = 1.3
SCREEN_NEAR = 2.7             # just outside the capture zone -- inside it is the holders'
SCREEN_REACH = 9.0            # job -- and inside blaster range of anything standing in it
# When the zone is held by a bigger fleet there is nothing to be gained by standing in its
# blaster range: the army waits out of reach, massed, while two bodies keep the payload
# frozen, and comes forward when the numbers are level.
STANDOFF_NEAR = 10.6
STANDOFF_REACH = 14.0
SCREEN_REFRESH = 25           # ticks between rebuilds of the lattice
SCREEN_BACK_BONUS = 5.0       # score penalty for a post on the enemy's side of the payload
SCREEN_COVER_BONUS = 7.0      # score bonus per spot in the zone the post can actually put
                              # a ray into: a post whose every line runs through the
                              # payload is worth less than one further out with an angle
MAX_POSTS = 30                # enough for a full fleet; nobody should be left postless
NEAR_PAYLOAD = 8.0            # radius used to compare fleet strength around the payload
SPLASH_SAFE = 1.25            # minimum spacing between two formation slots

# --- defence -------------------------------------------------------------------------
DEPOSIT_GUARD = 11.0          # enemies this close to our deposit pull defenders back
MAX_DEFENDERS = 3
SURVIVAL_FLEET = 3            # in the endgame, with the track won, this few bots left
                              # stop fighting and run: an empty fleet loses on the spot
WOUNDED_BACK = 400.0          # sorting nudge that sends one-hit-from-dead bots to the
                              # back of the formation, where the healers are

# --- gunnery -------------------------------------------------------------------------
AIM_SAFETY = 0.80             # fraction of the target's hull the shot must pass through
SWITCH_MARGIN = 2.5           # how much better a new target must score before we re-aim
PENALTY_LOAD = 2.6            # per ally already aiming at it: spread the guns out
PENALTY_INVULN = 4.0          # it cannot take damage this tick
BONUS_FINISHER = 4.0          # one more hit kills it
BONUS_HEALER = 3.0            # an enemy healer undoes exactly one blaster's damage
BONUS_EXTRACTOR = 1.0         # an enemy extractor is their economy
BONUS_CLUSTER = 1.5           # per enemy stacked next to it: splash hits them all
CLUSTER_RADIUS = 1.1
STOP_TO_AIM_DEG = 25.0        # stand still to let a turn settle when this close to aimed

# --- compute -------------------------------------------------------------------------
BUDGET_FLOOR = 120_000        # below this the tick drops the optional work
DEBUG_EVERY = 1500            # print a status line this often; 0 disables


# =====================================================================================
# Cross-tick memory
#
# The only things worth remembering: the mining stand-offs (a one-off geometry problem)
# and who each gun was aiming at (so slow turrets stop flip-flopping between targets).
# =====================================================================================

_MINE_SLOTS = None
_MINE_SLOTS_KEY = None
_SCREEN = None
_SCREEN_KEY = None
_AIM = {}


def get_strategy(team: int) -> Strategy:
    """Same brain on both sides -- the engine mirrors the world for the top-right team."""
    print(f"economy-first bot online (team {team})")
    return economy_strategy


# =====================================================================================
# Small helpers
# =====================================================================================

def _ang_norm(deg: float) -> float:
    """`deg` folded into [-180, 180)."""
    d = (deg + 180.0) % 360.0 - 180.0
    return d


def _popcount(mask: int) -> int:
    return bin(mask & 0xFFFFFFFF).count("1")


def _seg_blocked_by_disc(ax, ay, bx, by, cx, cy, radius) -> bool:
    """Does the disc at `c` sit on the segment a->b? The blaster ray stops at the payload
    and at either deposit, which `line_of_sight` does not model (it only checks walls)."""
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq <= 1e-9:
        return False
    t = ((cx - ax) * dx + (cy - ay) * dy) / len_sq
    if t <= 0.0 or t >= 1.0:
        return False  # the disc is behind the muzzle or past the target
    px = ax + dx * t
    py = ay + dy * t
    return (px - cx) ** 2 + (py - cy) ** 2 < radius * radius


def _screen_slots(payload, back_deg, payload_radius, marks, near, reach, tick, check):
    """Firing posts for everyone not holding the zone, best position first.

    A lattice rather than a ring. The payload track runs down corridors barely three
    tiles wide, so most of any ring around it is solid wall -- sampling a grid instead
    fills whatever shape the corridor actually is, and a lattice step wider than the
    splash diameter keeps the fleet un-stacked for free. Every post is required to see
    the payload, and to sit inside blaster range of anything standing in the zone.

    Recomputed only when the payload has moved half a tile. It crawls at 0.02 a tick, so
    this is a few hundred engine queries every 25 ticks, not every tick.
    """
    global _SCREEN, _SCREEN_KEY

    px, py = payload.x, payload.y
    # The lattice costs a few hundred engine queries, so it is rebuilt when the payload
    # has moved half a tile, when the band changes, and otherwise no more than once every
    # SCREEN_REFRESH ticks -- a burst the compute bank absorbs easily, against a refill it
    # would not survive every tick.
    key = (round(px * 2.0), round(py * 2.0), check, near, tick // SCREEN_REFRESH)
    if _SCREEN_KEY == key and _SCREEN is not None:
        return _SCREEN

    bx = math.cos(math.radians(back_deg))
    by = math.sin(math.radians(back_deg))
    near_sq = near * near
    reach_sq = reach * reach
    span = int(reach / SCREEN_STEP)

    scored = []
    for ix in range(-span, span + 1):
        dx = ix * SCREEN_STEP
        for iy in range(-span, span + 1):
            dy = iy * SCREEN_STEP
            d_sq = dx * dx + dy * dy
            if d_sq < near_sq or d_sq > reach_sq:
                continue          # inside the zone is the holders' job, beyond it is out of range
            q = Vec2(px + dx, py + dy)
            if check and (not point_free(q) or not line_of_sight(q, payload)):
                continue
            # Behind the payload, relative to where the enemy comes from, is the safer
            # half -- but only a preference: in a dead-end corridor it may be all wall.
            cover = 0
            for mx, my in marks:
                if not _seg_blocked_by_disc(q.x, q.y, mx, my, px, py, payload_radius):
                    cover += 1
            score = math.sqrt(d_sq) - SCREEN_COVER_BONUS * cover
            if dx * bx + dy * by <= 0.0:
                score += SCREEN_BACK_BONUS
            scored.append((score, q))

    scored.sort(key=lambda it: it[0])
    slots = [q for _, q in scored[:MAX_POSTS]]

    _SCREEN, _SCREEN_KEY = slots, key
    return slots


def _assign_slots(bots, slots):
    """Greedy nearest-slot assignment: `bots` come in priority order, id -> Vec2 out."""
    out = {}
    taken = [False] * len(slots)
    for b in bots:
        best = -1
        best_d = 1e18
        bx, by = b[1], b[2]
        for i, s in enumerate(slots):
            if taken[i]:
                continue
            d = (s.x - bx) ** 2 + (s.y - by) ** 2
            if d < best_d:
                best_d = d
                best = i
        if best < 0:
            break
        taken[best] = True
        out[b[0]] = slots[best]
    return out


def _mining_stands(deposit, conf, check):
    """Spread-out spots that can see our deposit, cached for the whole match.

    An extractor does not have to hug the ring: the mining ray reaches
    `base_extract_range` and only walls stop it, so standing back is free safety. We
    prefer the spots furthest from the map centre -- those sit deepest in our own half,
    furthest from where the enemy will walk in from.
    """
    global _MINE_SLOTS, _MINE_SLOTS_KEY

    key = (round(deposit.x, 2), round(deposit.y, 2))
    if _MINE_SLOTS is not None and _MINE_SLOTS_KEY == key:
        return _MINE_SLOTS

    hug = conf.deposit.radius + conf.bot.radius + 0.1
    reach = min(conf.bot.base_extract_range - conf.deposit.radius - 0.6, 4.0)
    centre_x = MAP_SIZE * 0.5
    centre_y = MAP_SIZE * 0.5

    cands = []
    r = hug + 0.4
    while r <= max(reach, hug + 0.4):
        n = max(8, int(2.0 * math.pi * r / 0.85))
        for k in range(n):
            a = 2.0 * math.pi * k / n
            p = Vec2(deposit.x + r * math.cos(a), deposit.y + r * math.sin(a))
            if p.x < 0.6 or p.y < 0.6 or p.x > MAP_SIZE - 0.6 or p.y > MAP_SIZE - 0.6:
                continue
            if check and (not point_free(p) or not line_of_sight(p, deposit)):
                continue
            cands.append(p)
        r += 0.9

    cands.sort(key=lambda p: -((p.x - centre_x) ** 2 + (p.y - centre_y) ** 2))

    picked = []
    gap = SPLASH_SAFE * SPLASH_SAFE
    for p in cands:
        if all((p.x - q.x) ** 2 + (p.y - q.y) ** 2 >= gap for q in picked):
            picked.append(p)
        if len(picked) >= 12:
            break

    if not picked:
        # Nothing validated (or we were too poor to check): hull to hull, on the side
        # facing away from the map centre.
        away = Vec2(deposit.x - centre_x, deposit.y - centre_y).normalize_or_zero()
        if away.x == 0.0 and away.y == 0.0:
            away = Vec2(0.0, 1.0)
        picked = [Vec2(deposit.x + away.x * hug, deposit.y + away.y * hug)]

    if check:
        _MINE_SLOTS = picked
        _MINE_SLOTS_KEY = key
    return picked


# =====================================================================================
# Gunnery
# =====================================================================================

def _aim_error(bx, by, ba, tx, ty, turn_speed):
    """(distance, aim error in degrees that will still be left after this tick's turn).

    The engine turns a bot *after* it moves, at most `turn_speed` degrees, aiming from
    the post-move position at the point we hand `turn_towards`. A bot that is standing
    still to shoot therefore ends this tick pointing `err - turn_speed` off target, and
    the shot is resolved with exactly that facing.
    """
    dx = tx - bx
    dy = ty - by
    d = math.sqrt(dx * dx + dy * dy)
    if d < 1e-6:
        return d, 180.0
    err = _ang_norm(math.degrees(math.atan2(dy, dx)) - ba)
    if err > turn_speed:
        err -= turn_speed
    elif err < -turn_speed:
        err += turn_speed
    else:
        err = 0.0
    return d, err


def _pick_target(bx, by, foes, load, prev_id, damage, blockers, reach):
    """Cheapest-first scoring over every enemy; lower is better.

    Distance carries the score and the rest are nudges measured in map units, so a
    finisher three tiles further away still beats a fresh target. Two things this
    deliberately does *not* score on: whether an ally already fired at it this tick,
    and anything else that flips from tick to tick -- a turret that turns 3 degrees a
    tick and re-picks every tick never finishes a turn and never fires. Spreading is
    done with `load`, which is stable because every fleet member walks this list in the
    same order every tick.
    """
    b1 = b2 = b3 = None
    s1 = s2 = s3 = 1e18
    prev_score = 1e18
    for f in foes:
        dx = f[1] - bx
        dy = f[2] - by
        score = math.sqrt(dx * dx + dy * dy) + PENALTY_LOAD * load.get(f[0], 0)
        if not f[6]:
            score += PENALTY_INVULN
        if f[5] <= damage:
            score -= BONUS_FINISHER
        elif f[7] == BotClass.Healer:
            score -= BONUS_HEALER
        elif f[7] == BotClass.Extractor:
            score -= BONUS_EXTRACTOR
        score -= BONUS_CLUSTER * f[8]
        if f[0] == prev_id:
            prev_score = score
        if score < s1:
            s3, b3 = s2, b2
            s2, b2 = s1, b1
            s1, b1 = score, f
        elif score < s2:
            s3, b3 = s2, b2
            s2, b2 = score, f
        elif score < s3:
            s3, b3 = score, f

    # Hysteresis: only re-aim when the new pick is clearly better.
    if prev_score < s1 + SWITCH_MARGIN and b1 is not None and b1[0] != prev_id:
        for f in foes:
            if f[0] == prev_id:
                return f, _shot_blocked(bx, by, f[3], f[4], blockers, reach)

    # The payload and both deposits stop a blaster ray, and so does any wall. Prefer a
    # target we can actually reach over the nearest one, rather than spending the fight
    # aiming at the thing we are escorting.
    for cand in (b1, b2, b3):
        if cand is None:
            continue
        if not _shot_blocked(bx, by, cand[3], cand[4], blockers, reach):
            return cand, False
    return b1, True


def _shot_blocked(bx, by, tx, ty, blockers, reach):
    for cx, cy, r in blockers:
        if _seg_blocked_by_disc(bx, by, tx, ty, cx, cy, r):
            return True
    # Only worth an engine query for a target we could actually shoot this tick.
    if (tx - bx) ** 2 + (ty - by) ** 2 <= reach * reach:
        return not line_of_sight(Vec2(bx, by), Vec2(tx, ty))
    return False


# =====================================================================================
# The strategy
# =====================================================================================

def economy_strategy(state: GameState) -> FleetAction:
    """Entry point. Never let a bug idle the whole fleet: fall back to something dumb
    but functional if the real plan throws."""
    try:
        return _plan(state)
    except Exception as exc:  # pragma: no cover - insurance, not logic
        print(f"[strategy] tick {state.tick}: {type(exc).__name__}: {exc}")
        return _panic_plan(state)


def _plan(state: GameState) -> FleetAction:
    conf = get_config()
    bot_conf = conf.bot
    tick = state.tick
    action = FleetAction.new()

    in_endgame = tick >= conf.max_ticks - conf.endgame_ticks
    # Spend the optional compute only while the bank is comfortable. Sitting out ticks
    # costs far more than a slightly worse plan does.
    rich = get_budget().remaining > BUDGET_FLOOR

    payload = state.payload_pos()
    px, py = payload.x, payload.y
    capture_r = conf.payload.capture_radius
    deposit = state.deposit_me.pos
    turn_speed = bot_conf.turn_speed
    blaster_range = bot_conf.blaster_range
    damage = bot_conf.blaster_damage
    max_health = bot_conf.health

    # --- snapshots -------------------------------------------------------------------
    # ctypes field reads are not free, so everything the loops below need is pulled into
    # plain Python numbers exactly once.
    #   ally: [id, x, y, angle, health, class, ready]
    #   foe:  [id, x, y, pred_x, pred_y, health, vulnerable, class, cluster]
    allies = []
    n_ext = n_heal = n_batt = 0
    for b in state.fleet_me:
        cls = b.class_
        p = b.pos
        allies.append([b.id, p.x, p.y, b.angle, b.health, cls, b.next_fire_tick <= tick])
        if cls == BotClass.Extractor:
            n_ext += 1
        elif cls == BotClass.Healer:
            n_heal += 1
        else:
            n_batt += 1

    foes = []
    for e in state.fleet_other:
        p = e.pos
        v = e.vel
        foes.append([e.id, p.x, p.y, p.x + v.x, p.y + v.y, e.health,
                     e.invulnerable_until_tick <= tick, e.class_, 0])

    # Stacked enemies share a blast, so a shot into a clump is worth several shots.
    if rich and len(foes) > 1:
        cr2 = CLUSTER_RADIUS * CLUSTER_RADIUS
        for i in range(len(foes)):
            fi = foes[i]
            for j in range(i + 1, len(foes)):
                fj = foes[j]
                dx = fi[1] - fj[1]
                dy = fi[2] - fj[2]
                if dx * dx + dy * dy <= cr2:
                    fi[8] += 1
                    fj[8] += 1

    # --- how the payload fight stands -------------------------------------------------
    cap2 = capture_r * capture_r
    near2 = NEAR_PAYLOAD * NEAR_PAYLOAD
    my_zone = my_near = 0
    for a in allies:
        d = (a[1] - px) ** 2 + (a[2] - py) ** 2
        if d <= cap2:
            my_zone += 1
        if d <= near2:
            my_near += 1
    foe_zone = foe_near = 0
    for f in foes:
        d = (f[1] - px) ** 2 + (f[2] - py) ** 2
        if d <= cap2:
            foe_zone += 1
        if d <= near2:
            foe_near += 1

    # Press when we are not out-gunned around the payload, when nobody is home to stop
    # us, or when our whole army is at least as big as theirs. Otherwise hold the
    # minimum that keeps the payload frozen, keep the rest out of blaster range, and let
    # the economy do the work -- a frozen payload is a draw we win later.
    army_me = n_batt + n_heal
    pressing = foe_near == 0 or my_near >= foe_near or army_me >= len(foes)

    # --- roles -------------------------------------------------------------------------
    extractors = [a for a in allies if a[5] == BotClass.Extractor]
    healers = [a for a in allies if a[5] == BotClass.Healer]
    battle = [a for a in allies if a[5] == BotClass.Battle]

    # Raiders in our own half pull the nearest guns off the line: extractors are the
    # whole plan, and they cannot defend themselves.
    defender_ids = set()
    if battle and extractors and not in_endgame:
        threats = 0
        g2 = DEPOSIT_GUARD * DEPOSIT_GUARD
        for f in foes:
            if (f[1] - deposit.x) ** 2 + (f[2] - deposit.y) ** 2 <= g2:
                threats += 1
        if threats:
            want = min(MAX_DEFENDERS, threats + 1, len(battle))
            home = sorted(battle,
                          key=lambda a: (a[1] - deposit.x) ** 2 + (a[2] - deposit.y) ** 2)
            defender_ids = {a[0] for a in home[:want]}

    # --- formation around the payload --------------------------------------------------
    force = [a for a in battle if a[0] not in defender_ids]
    force.extend(healers)
    if in_endgame:
        # Nothing left to mine for: a body in the capture zone is the only currency in
        # the last third of the match.
        force.extend(extractors)
    # Closest to the payload first -- they take the in-zone posts. A bot one hit from
    # dead sorts to the back instead, where it is out of the splash and inside the
    # healers' range. Only while we are winning the fight, though: rotating a gun out of
    # a line that is already outnumbered just gets the rest of the line killed faster.
    wounded_back = WOUNDED_BACK if pressing else 0.0
    force.sort(key=lambda a: (a[1] - px) ** 2 + (a[2] - py) ** 2
               + (wounded_back if a[4] <= damage else 0.0))

    # Elimination during the endgame is an instant loss whatever the payload is doing.
    # With the track won and only a couple of bodies left, surviving to the clock wins
    # the match that one more fight would throw away.
    capture = state.capture
    survival = in_endgame and capture > 0.02 and len(allies) <= SURVIVAL_FLEET
    safe_corner = Vec2(bot_conf.radius + 0.01, MAP_SIZE - bot_conf.radius - 0.01)

    back = payload_pos(max(-1.0, capture - 0.08))
    fwd = payload_pos(min(1.0, capture + 0.08))
    bvx, bvy = back.x - px, back.y - py
    fvx, fvy = fwd.x - px, fwd.y - py
    if bvx * bvx + bvy * bvy < 1e-6:
        bvx, bvy = deposit.x - px, deposit.y - py
    if fvx * fvx + fvy * fvy < 1e-6:
        fvx, fvy = -bvx, -bvy
    back_deg = math.degrees(math.atan2(bvy, bvx))
    fwd_deg = math.degrees(math.atan2(fvy, fvx))

    want_hold = HOLD_WHEN_PRESSING if pressing else HOLD_WHEN_OUTGUNNED
    want_hold = max(1, min(want_hold, len(force)))

    hold_slots = []
    gap = SPLASH_SAFE * SPLASH_SAFE
    for radius in HOLD_RINGS:
        for off in HOLD_OFFSETS:
            a = math.radians(fwd_deg + off)
            p = Vec2(px + radius * math.cos(a), py + radius * math.sin(a))
            if rich and not point_free(p):
                continue
            if all((p.x - s.x) ** 2 + (p.y - s.y) ** 2 >= gap for s in hold_slots):
                hold_slots.append(p)
        if len(hold_slots) >= want_hold + 2:
            break
    if not hold_slots:
        hold_slots = [Vec2(px + bvx * 0.35, py + bvy * 0.35)]

    # Judge a firing post on whether it can put a ray into where the enemy actually is.
    # A fleet that camps in the payload's shadow is immune to anything shooting from
    # directly behind it, and those are the fleets worth beating.
    marks = []
    for f in sorted(foes, key=lambda f: (f[1] - px) ** 2 + (f[2] - py) ** 2)[:5]:
        if (f[1] - px) ** 2 + (f[2] - py) ** 2 <= 49.0:
            marks.append((f[1], f[2]))
    if not marks:
        fux, fuy = fvx / max(math.hypot(fvx, fvy), 1e-6), fvy / max(math.hypot(fvx, fvy), 1e-6)
        marks = [(px + fux * 1.7, py + fuy * 1.7),
                 (px - fuy * 1.8, py + fux * 1.8),
                 (px + fuy * 1.8, py - fux * 1.8)]
    near = SCREEN_NEAR if pressing else STANDOFF_NEAR
    reach = SCREEN_REACH if pressing else STANDOFF_REACH
    screen_slots = _screen_slots(payload, back_deg, conf.payload.radius, tuple(marks),
                                 near, reach, tick, rich)
    if not screen_slots:
        screen_slots = hold_slots

    posts = _assign_slots(force[:want_hold], hold_slots)
    posts.update(_assign_slots(force[want_hold:], screen_slots))
    holder_ids = {a[0] for a in force[:want_hold]}
    # Anyone the slots did not cover still walks to the fight rather than standing at
    # the spawn: a body on the way is a body that can take a post as others die.
    fallback_post = Vec2(px + bvx * 0.55, py + bvy * 0.55)

    # --- mining stands -----------------------------------------------------------------
    stands = _mining_stands(deposit, conf, rich) if extractors else []

    # =================================================================================
    # Orders
    # =================================================================================
    claimed = set()      # enemies already being shot at this tick: one blast per bot per
                         # tick is all the invulnerability window allows
    aim_load = {}        # enemies already being aimed at: spreads the guns around
    heal_load = {}
    heal_cap = int(bot_conf.heal_stack_cap)
    other_dep = state.deposit_other.pos
    blockers = ((px, py, conf.payload.radius),
                (deposit.x, deposit.y, conf.deposit.radius),
                (other_dep.x, other_dep.y, conf.deposit.radius))

    for a in battle:
        bid, bx, by, ba, hp, _cls, ready = a
        act = action.bots[bid]

        target = None
        blocked = True
        if foes:
            target, blocked = _pick_target(bx, by, foes, aim_load, _AIM.get(bid),
                                           damage, blockers, blaster_range)

        fire = False
        stop_to_aim = False
        if target is not None:
            _AIM[bid] = target[0]
            aim_load[target[0]] = aim_load.get(target[0], 0) + 1
            tx, ty = target[3], target[4]  # where it will be when the ray is resolved
            act.turn_action = turn_towards(Vec2(tx, ty))
            dist, err = _aim_error(bx, by, ba, tx, ty, turn_speed)
            if ready and dist <= blaster_range - 0.1 and not blocked:
                aimed = abs(err) <= math.degrees(math.asin(
                    min(1.0, bot_conf.radius * AIM_SAFETY / max(dist, 1e-3))))
                if aimed:
                    # `blocked` already covered walls and discs for this target.
                    if target[6] and target[0] not in claimed:
                        fire = True
                        claimed.add(target[0])
                elif abs(err) <= STOP_TO_AIM_DEG:
                    # Close enough that standing still finishes the turn in a few ticks;
                    # walking would keep dragging the sights off the target.
                    stop_to_aim = True
        else:
            # Nobody to shoot: face the way the enemy has to arrive from, so the first
            # contact does not start with a 60 tick turn.
            act.turn_action = turn_to_angle(fwd_deg)

        act.special_action = SpecialAction.Battle(fire=fire)

        if survival:
            post = safe_corner
        elif bid in defender_ids:
            # Stand off the deposit, between it and whoever is coming.
            post = stands[0] if stands else deposit
        else:
            post = posts.get(bid, fallback_post)
        # A holder that is not in the zone yet keeps walking: presence freezes the
        # payload, and one shot is worth less than that.
        must_reach = (bid in holder_ids and not survival
                      and (bx - px) ** 2 + (by - py) ** 2 > cap2 * 0.81)
        if (fire or stop_to_aim) and not must_reach:
            pass  # hold still: the shot is worth more than 0.05 of a tile
        else:
            act.move_action = move_bot(navigate_to(Vec2(bx, by), post))

    # --- healers -----------------------------------------------------------------------
    heal_range = bot_conf.base_heal_range
    for a in healers:
        bid, bx, by, ba, hp, _cls, _ready = a
        act = action.bots[bid]

        best = None
        best_frac = 2.0
        for o in allies:
            if o[0] == bid or o[4] >= max_health - 1e-3:
                continue
            if heal_load.get(o[0], 0) >= heal_cap:
                continue
            dx = o[1] - bx
            dy = o[2] - by
            if dx * dx + dy * dy > (heal_range - 0.15) ** 2:
                continue
            frac = o[4] / max_health
            if frac < best_frac:
                best_frac = frac
                best = o

        if best is not None and line_of_sight(Vec2(bx, by), Vec2(best[1], best[2])):
            heal_load[best[0]] = heal_load.get(best[0], 0) + 1
            act.turn_action = turn_towards(Vec2(best[1], best[2]))
            act.special_action = SpecialAction.Healer(fire=True, target=best[0])
            # Creep along with the patient instead of sitting at the edge of the arc.
            dx = best[1] - bx
            dy = best[2] - by
            if dx * dx + dy * dy > (heal_range * 0.6) ** 2:
                act.move_action = move_bot(navigate_to(Vec2(bx, by), Vec2(best[1], best[2])))
        else:
            act.special_action = SpecialAction.Healer(fire=False, target=bid)
            act.turn_action = turn_to_angle(fwd_deg)
            act.move_action = move_bot(navigate_to(
                Vec2(bx, by), safe_corner if survival else posts.get(bid, fallback_post)))

    # --- extractors ---------------------------------------------------------------------
    for idx, a in enumerate(extractors):
        bid, bx, by, ba, hp, _cls, _ready = a
        act = action.bots[bid]
        if in_endgame:
            post = safe_corner if survival else posts.get(bid, fallback_post)
            act.move_action = move_bot(navigate_to(Vec2(bx, by), post))
            act.turn_action = turn_to_angle(fwd_deg)
            act.special_action = SpecialAction.Extractor(mine=False)
            continue
        stand = stands[idx % len(stands)] if stands else deposit
        act.move_action = move_bot(navigate_to(Vec2(bx, by), stand))
        act.turn_action = turn_towards(deposit)
        act.special_action = SpecialAction.Extractor(mine=True)

    # =================================================================================
    # Fabricator
    # =================================================================================
    # Both the free build and a rush order use `fabricator_next`, so this one choice is
    # the whole build order. A bot bought this tick only shows up in the fleet next
    # tick, so the class we asked for last tick is counted in as pending -- otherwise
    # the opening buys one extractor too many.
    pending = _AIM.pop("_pending", None)
    if pending is not None and pending[0] == tick - 1:
        if pending[1] == BotClass.Extractor:
            n_ext += 1
        elif pending[1] == BotClass.Healer:
            n_heal += 1
        else:
            n_batt += 1

    # Slots are shared with the enemy, so do not buy miners for a deposit they have
    # already filled.
    free_slots = conf.deposit.extractor_cap - _popcount(state.deposit_me.extractors.other)
    want_ext = OPENING_EXTRACTORS if tick < 200 else STEADY_EXTRACTORS
    want_ext = max(0, min(want_ext, free_slots))
    if tick > conf.max_ticks - conf.endgame_ticks - EXTRACTOR_PAYBACK_TICKS:
        want_ext = 0  # too late for one to earn its rush cost back

    want_heal = 0
    if n_batt >= HEALER_UNLOCK:
        want_heal = min(MAX_HEALERS, n_batt // HEALER_PER_BATTLE)

    # Trade mining slots for guns while there is still time to build them: at a full
    # fleet an extractor is a body that cannot shoot, and after the fabricator shuts off
    # its tokens buy nothing at all. The bot still acts on the tick it blows itself up,
    # and the freed slot is filled by next tick's rush order.
    cashing_out = (not in_endgame
                   and tick >= conf.max_ticks - conf.endgame_ticks - CASHOUT_TICKS
                   and n_ext > 0
                   and state.fleet_me.is_full()
                   and state.fabricator_me.tokens >= conf.fabricator.rush_cost)
    if cashing_out:
        victim = min(extractors, key=lambda a: a[4])
        action.bots[victim[0]].self_destruct = True
        next_class = BotClass.Battle
    elif n_ext < want_ext:
        next_class = BotClass.Extractor
    elif n_heal < want_heal:
        next_class = BotClass.Healer
    else:
        next_class = BotClass.Battle
    action.fabricator_next = int(next_class)

    # Tokens buy nothing but rush orders and are only the third tiebreaker, so they are
    # spent the moment they can be. Nothing is built in the endgame, and a rush into a
    # full fleet is refused, so neither is worth asking for.
    can_rush = (not in_endgame
                and not state.fleet_me.is_full()
                and state.fabricator_me.tokens >= conf.fabricator.rush_cost)
    action.rush_order = can_rush
    if can_rush:
        _AIM["_pending"] = (tick, next_class)

    if DEBUG_EVERY and tick % DEBUG_EVERY == 0:
        print(f"t{tick} cap={capture:+.3f} fleet={len(allies)}"
              f"(E{n_ext}/H{n_heal}/B{n_batt}) vs {len(foes)}"
              f" zone={my_zone}-{foe_zone} tok={state.fabricator_me.tokens:.0f}"
              f" {'press' if pressing else 'hold'}")

    return action


def _panic_plan(state: GameState) -> FleetAction:
    """Last resort. Mine, walk at the payload, shoot what is in front of us."""
    action = FleetAction.new()
    try:
        conf = get_config()
        payload = state.payload_pos()
        deposit = state.deposit_me.pos
        rng = conf.bot.blaster_range
        for bot in state.fleet_me:
            act = action.bots[bot.id]
            if bot.class_ == BotClass.Extractor:
                act.move_action = move_bot(navigate_to(bot.pos, deposit))
                act.turn_action = turn_towards(deposit)
                act.special_action = SpecialAction.Extractor(mine=True)
                continue
            act.move_action = move_bot(navigate_to(bot.pos, payload))
            nearest = None
            nd = 1e18
            for e in state.fleet_other:
                d = bot.pos.dist_sq(e.pos)
                if d < nd:
                    nd = d
                    nearest = e.pos
            if nearest is None:
                continue
            act.turn_action = turn_towards(nearest)
            if bot.class_ == BotClass.Battle:
                act.special_action = SpecialAction.Battle(
                    fire=nd <= rng * rng and line_of_sight(bot.pos, nearest))
        action.fabricator_next = int(BotClass.Battle)
        action.rush_order = state.fabricator_me.tokens >= conf.fabricator.rush_cost
    except Exception:
        pass
    return action

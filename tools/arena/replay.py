#!/usr/bin/env python3
"""Replay a MechMania gamelog (.mmgl) and extract per-team metrics.

    python3 replay.py summary LOG [a|b]      one JSON line (metrics from that side's view)
    python3 replay.py table LOG [--window N] per-window shots / hits / losses / heals
    python3 replay.py timeline LOG [--every N] payload fight: who holds the circle, distances

The log is the engine's own output: a config line, a full state, then per-tick diffs, and
`# result: {...}` at the end.  Counted here:
  shots   ticks a battle bot's `shot` became Some
  hits    an enemy's invulnerable_until_tick changed (one per blast landed on a bot)
  lost    bots removed; built: bots added (both per class)
  healed  health gained
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict

PATH = [(16, 16), (23, 16), (23, 11), (9, 11), (9, 6), (23, 6), (23, 3)]


def _cls(bot):
    return next(iter(bot["special"]))


def replay(path, window=None):
    fh = open(path)
    json.loads(fh.readline())  # config
    first = json.loads(fh.readline())
    fleets = {s: {b["id"]: b for b in first[f"fleet_{s}"]} for s in "ab"}
    fab = {s: dict(first[f"fabricator_{s}"]) for s in "ab"}
    capture = first.get("capture", 0.0)
    tick = first.get("tick", 0)
    tot = {s: defaultdict(float) for s in "ab"}
    built = {s: Counter() for s in "ab"}
    lost = {s: Counter() for s in "ab"}
    win = defaultdict(lambda: {s: defaultdict(float) for s in "ab"})
    result = {}
    for line in fh:
        if line.startswith("#"):
            m = re.search(r"# result: (\{.*\})", line)
            if m:
                result = json.loads(m.group(1))
            continue
        d = json.loads(line)
        tick = d.get("tick", tick)
        capture = d.get("capture", capture)
        w = (tick // window) * window if window else 0
        for s in "ab":
            if f"fabricator_{s}" in d:
                fab[s].update(d[f"fabricator_{s}"])
            fd = d.get(f"fleet_{s}")
            if not fd:
                continue
            fl = fleets[s]
            for bid, bot in (fd.get("added") or {}).items():
                fl[int(bid)] = bot
                built[s][_cls(bot)] += 1
            for bid, ch in (fd.get("changed") or {}).items():
                bot = fl[int(bid)]
                if "health" in ch:
                    delta = ch["health"] - bot["health"]
                    if delta > 0:
                        tot[s]["healed"] += delta
                        win[w][s]["healed"] += delta
                if "invulnerable_until_tick" in ch:
                    other = "b" if s == "a" else "a"
                    tot[other]["hits"] += 1
                    win[w][other]["hits"] += 1
                sp = ch.get("special", {})
                if "Battle" in sp and isinstance(sp["Battle"].get("shot"), dict):
                    tot[s]["shots"] += 1
                    win[w][s]["shots"] += 1
                for k, v in ch.items():
                    if k == "special":
                        c = next(iter(v))
                        bot["special"].setdefault(c, {}).update(v[c])
                    else:
                        bot[k] = v
            for bid in fd.get("removed") or []:
                bot = fl.pop(int(bid), None)
                if bot:
                    lost[s][_cls(bot)] += 1
                    win[w][s]["lost"] += 1
    alive = {s: Counter(_cls(b) for b in fleets[s].values()) for s in "ab"}
    return {
        "result": result,
        "tick": tick,
        "capture": capture,
        "tot": tot,
        "built": built,
        "lost": lost,
        "alive": alive,
        "tokens": {s: fab[s].get("tokens", 0.0) for s in "ab"},
        "windows": win,
    }


def payload_xy(capture):
    """Payload centre for a capture value (A pushes it along PATH, B along the mirror)."""
    left = abs(capture) * 48.0
    x, y = PATH[0]
    for nx, ny in PATH[1:]:
        seg = abs(nx - x) + abs(ny - y)
        if left <= seg:
            x += (nx - x) * left / seg
            y += (ny - y) * left / seg
            break
        left -= seg
        x, y = nx, ny
    return (x, y) if capture >= 0 else (32.0 - x, 32.0 - y)


def timeline(path, every=100):
    """Every N ticks: capture, bots of each side inside the capture circle, and each side's
    fighters within 6 / 12 of the payload plus their median distance to it."""
    fh = open(path)
    json.loads(fh.readline())
    first = json.loads(fh.readline())
    fleets = {s: {b["id"]: b for b in first[f"fleet_{s}"]} for s in "ab"}
    capture, tick, nxt = first.get("capture", 0.0), first.get("tick", 0), 0
    print(f"{'tick':>5} {'cap':>6} | {'A in':>4} {'A<6':>4} {'A<12':>4} {'A med':>5} {'A B/H/E':>8} | "
          f"{'B in':>4} {'B<6':>4} {'B<12':>4} {'B med':>5} {'B B/H/E':>8}")

    def row():
        px, py = payload_xy(capture)
        out = [f"{tick:>5} {capture:>+6.3f} |"]
        for s in "ab":
            bots = list(fleets[s].values())
            d = [((b["pos"]["x"] - px) ** 2 + (b["pos"]["y"] - py) ** 2) ** 0.5 for b in bots]
            fd = sorted(di for di, b in zip(d, bots) if _cls(b) != "Extractor")
            cnt = Counter(_cls(b)[0] for b in bots)
            med = fd[len(fd) // 2] if fd else float("nan")
            out.append(f"{sum(di <= 2.5 for di in d):>4} {sum(x <= 6 for x in fd):>4} {sum(x <= 12 for x in fd):>4} "
                       f"{med:>5.1f} {cnt['B']:>2}/{cnt['H']:>2}/{cnt['E']:>2} |")
        print(" ".join(out).rstrip("|"))

    for line in fh:
        if line.startswith("#"):
            continue
        d = json.loads(line)
        tick = d.get("tick", tick)
        capture = d.get("capture", capture)
        for s in "ab":
            fd = d.get(f"fleet_{s}")
            if not fd:
                continue
            fl = fleets[s]
            for bid, bot in (fd.get("added") or {}).items():
                fl[int(bid)] = bot
            for bid, ch in (fd.get("changed") or {}).items():
                bot = fl[int(bid)]
                for k, v in ch.items():
                    if k == "pos":
                        bot["pos"].update(v)
                    elif k == "special":
                        c = next(iter(v))
                        bot["special"].setdefault(c, {}).update(v[c])
                    else:
                        bot[k] = v
            for bid in fd.get("removed") or []:
                fl.pop(int(bid), None)
        if tick >= nxt:
            row()
            nxt = tick + every
    row()


def summary(path, side="a"):
    r = replay(path)
    other = "b" if side == "a" else "a"
    res = r["result"]
    winner = res.get("winner")
    outcome = "D" if winner is None else ("W" if winner.lower() == side else "L")
    cap = r["capture"] if side == "a" else -r["capture"]
    me, op = r["tot"][side], r["tot"][other]
    return {
        "outcome": outcome,
        "reason": res.get("reason"),
        "tick": res.get("tick", r["tick"]),
        "at_9000": res.get("tick", r["tick"]) >= 9000,
        "capture": round(cap, 3),
        "alive": dict(r["alive"][side]),
        "alive_enemy": dict(r["alive"][other]),
        "kills": sum(r["lost"][other].values()),
        "losses": sum(r["lost"][side].values()),
        "shots": int(me["shots"]),
        "hits": int(me["hits"]),
        "enemy_shots": int(op["shots"]),
        "healed": round(me["healed"], 1),
        "enemy_healed": round(op["healed"], 1),
        "built": dict(r["built"][side]),
        "tokens_end": round(r["tokens"][side], 1),
    }


def table(path, window=1000):
    r = replay(path, window)
    print(r["result"])
    print(f"{'window':>6} | {'A shots':>7} {'A hits':>6} {'A lost':>6} {'A heal':>7} | "
          f"{'B shots':>7} {'B hits':>6} {'B lost':>6} {'B heal':>7}")
    for w in sorted(r["windows"]):
        a, b = r["windows"][w]["a"], r["windows"][w]["b"]
        print(f"{w:>6} | {a['shots']:>7.0f} {a['hits']:>6.0f} {a['lost']:>6.0f} {a['healed']:>7.1f} | "
              f"{b['shots']:>7.0f} {b['hits']:>6.0f} {b['lost']:>6.0f} {b['healed']:>7.1f}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "summary":
        print(json.dumps(summary(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "a")))
    elif cmd == "timeline":
        every = int(sys.argv[sys.argv.index("--every") + 1]) if "--every" in sys.argv else 100
        timeline(sys.argv[2], every)
    elif cmd == "table":
        win = int(sys.argv[sys.argv.index("--window") + 1]) if "--window" in sys.argv else 1000
        table(sys.argv[2], win)
    else:
        sys.exit(__doc__)

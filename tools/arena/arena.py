#!/usr/bin/env python3
"""Reproducible regression matches with the real engine.

    python3 tools/arena/arena.py NAME=BOT[:ENV] ... [--opps key|quick|full|tag:raid|a,b]
                                 [--repeat N] [-j JOBS] [--baseline NAME] [--out FILE]

BOT is a directory under tools/arena/bots/ (e.g. baseline_v12) or `live` (this repo's
strategy/ as it is on disk now).  ENV is `K=V;K=V` overrides (the strategy reads IAMABOT_*).
Every candidate plays every selected opponent on both sides (A = bottom-left, B = mirrored),
`--repeat` times.  One line per finished game is printed as it lands (so `| tee` / `tail -f`
works), then a summary per candidate and, with --baseline, the games where the candidate and
the baseline disagree.

Needs the engine and native library that `mm-cli run` builds: .mm/bin/mm-engine and
native/target/release/libmm_python_native.{dylib,so}.  Run `mm-cli run` once if missing.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from roster import KEY, QUICK, ROSTER  # noqa: E402

ENGINE = ROOT / ".mm" / "bin" / "mm-engine"
WORK = HERE / ".work"


def native_lib() -> Path:
    for name in ("libmm_python_native.dylib", "libmm_python_native.so"):
        p = ROOT / "native" / "target" / "release" / name
        if p.exists():
            return p
    sys.exit("native library missing: run `mm-cli run` once in the repo root")


def assemble(bot: str) -> Path:
    """A runnable bot directory: this repo's core/ + __main__.py + the bot's strategy/."""
    dst = WORK / "bots" / bot
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    shutil.copytree(ROOT / "core", dst / "core", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "__main__.py", dst / "__main__.py")
    src = ROOT / "strategy" if bot == "live" else HERE / "bots" / bot / "strategy"
    if not src.is_dir():
        sys.exit(f"unknown bot {bot!r}: no {src}")
    shutil.copytree(src, dst / "strategy", ignore=shutil.ignore_patterns("__pycache__", "*.md"))
    return dst


def launcher(bot: str, env: str, lib: Path) -> Path:
    d = WORK / "launch"
    d.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9_]", "_", f"{bot}__{env}")[:180]
    path = d / f"{tag}.sh"
    lines = ["#!/bin/sh", f"export MM_NATIVE_LIB='{lib}'", "export PYTHONDONTWRITEBYTECODE=1",
             "export IAMABOT_STATS=1"]
    for kv in filter(None, env.split(",")):
        k, v = kv.split("=", 1)
        lines.append(f"export {k}='{v}'")
    lines.append(f"exec python3 '{WORK / 'bots' / bot / '__main__.py'}' \"$1\"")
    # Atomic replace: another thread's engine may be exec'ing this very launcher.
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".l")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)
    return path


def last_stats(bots_out: Path, side: str) -> dict:
    """The last `[stats] {...}` line our bot printed (IAMABOT_STATS=1)."""
    tag = f"#[{side.upper()}]: [stats] "
    stats = {}
    try:
        for line in bots_out.read_text(errors="replace").splitlines():
            if line.startswith(tag):
                try:
                    stats = json.loads(line[len(tag):])
                except ValueError:
                    pass
    except OSError:
        pass
    return stats


def play(cand, cbot, cenv, opp, obot, oenv, we_a, rep, lib, keep):
    a, ea = (cbot, cenv) if we_a else (obot, oenv)
    b, eb = (obot, oenv) if we_a else (cbot, cenv)
    la, lb = launcher(a, ea, lib), launcher(b, eb, lib)
    logs = WORK / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    glog = logs / f"{cand}_vs_{opp}_{'A' if we_a else 'B'}{rep}_{time.time_ns()}.mmgl"
    bots_out = Path(f"{glog}.bots")
    t0 = time.time()
    subprocess.run(
        [str(ENGINE), str(la), str(lb), "-o", f"g:{glog}", "-o", f"a,ae,b,be:{bots_out}"],
        capture_output=True, text=True, timeout=1800,
    )
    side = "a" if we_a else "b"
    out = subprocess.run(
        [sys.executable, str(HERE / "replay.py"), "summary", str(glog), side],
        capture_output=True, text=True,
    )
    try:
        m = json.loads(out.stdout)
    except ValueError:
        m = {"outcome": "?", "reason": "no-result", "tick": -1}
    m["stats"] = last_stats(bots_out, side)
    m.update(cand=cand, opp=opp, side=side.upper(), rep=rep, secs=round(time.time() - t0, 1))
    if not keep:
        glog.unlink(missing_ok=True)
        bots_out.unlink(missing_ok=True)
    else:
        m["log"] = str(glog)
    return m


def pick_opponents(spec: str) -> list:
    if spec == "key":
        return list(KEY)
    if spec == "quick":
        return list(QUICK)
    if spec == "full":
        return list(ROSTER)
    if spec.startswith("tag:"):
        tag = spec[4:]
        return [n for n, (_, _, tags) in ROSTER.items() if tag in tags]
    names = [s for s in spec.split(",") if s]
    for n in names:
        if n not in ROSTER:
            sys.exit(f"unknown opponent {n!r}")
    return names


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cands", nargs="+", help="NAME=BOT[:K=V;K=V]")
    ap.add_argument("--opps", default="quick")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("-j", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--baseline", default="")
    ap.add_argument("--baseline-file", default="", help="JSONL from an earlier run; its --baseline rows are reused instead of replaying")
    ap.add_argument("--out", default=str(HERE / "results" / "latest.jsonl"))
    ap.add_argument("--keep", action="store_true", help="keep gamelogs in tools/arena/.work/logs")
    args = ap.parse_args()
    if not ENGINE.exists():
        sys.exit(f"engine missing ({ENGINE}): run `mm-cli run` once in the repo root")
    lib = native_lib()
    cands = {}
    for c in args.cands:
        name, _, rest = c.partition("=")
        bot, _, env = (rest or name).partition(":")
        cands[name] = (bot, env.replace(";", ","))
    opps = pick_opponents(args.opps)
    for bot in {b for b, _ in cands.values()} | {ROSTER[o][0] for o in opps}:
        assemble(bot)
    jobs = [
        (cn, cb, ce, on, ROSTER[on][0], ROSTER[on][1], we_a, rep)
        for cn, (cb, ce) in cands.items()
        for on in opps
        for rep in range(args.repeat)
        for we_a in (True, False)
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    out = open(args.out, "w", buffering=1)
    print(f"# {len(jobs)} games: {list(cands)} x {len(opps)} opponents x 2 sides x {args.repeat}", flush=True)
    res = defaultdict(list)
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.j) as pool:
        futs = [pool.submit(play, *job, lib, args.keep) for job in jobs]
        for i, fut in enumerate(cf.as_completed(futs), 1):
            m = fut.result()
            res[m["cand"]].append(m)
            out.write(json.dumps(m) + "\n")
            st = m.get("stats", {})
            print(
                f"[{i:>4}/{len(jobs)}] {m['cand']:<10} vs {m['opp']:<11} {m['side']}{m['rep']} "
                f"{m['outcome']} {m.get('reason')}@{m.get('tick')} cap={m.get('capture', 0):+.2f} "
                f"K/L={m.get('kills')}/{m.get('losses')} "
                f"minbank={st.get('min_bank', '-')} fb={st.get('fallbacks', '-')}",
                flush=True,
            )
    print(f"\n({len(jobs)} games, {time.time() - t0:.0f}s) results: {args.out}")
    print(f"{'candidate':<12} {'W':>3} {'D':>3} {'L':>3}  {'@9000':>5} {'avg tick':>8} "
          f"{'K/L':>9} {'shots':>6} {'hit%':>5} {'minbank':>8} {'fallback':>8}")
    for cn in cands:
        rows = res[cn]
        w = sum(r["outcome"] == "W" for r in rows)
        d = sum(r["outcome"] == "D" for r in rows)
        l = sum(r["outcome"] == "L" for r in rows)
        at9 = sum(bool(r.get("at_9000")) for r in rows)
        avg_t = sum(r.get("tick", 0) for r in rows) / max(1, len(rows))
        k = sum(r.get("kills", 0) for r in rows)
        lo = sum(r.get("losses", 0) for r in rows)
        sh = sum(r.get("shots", 0) for r in rows)
        hi = sum(r.get("hits", 0) for r in rows)
        mb = min((r["stats"].get("min_bank", 10**9) for r in rows if r.get("stats")), default=None)
        fb = sum(r.get("stats", {}).get("fallbacks", 0) for r in rows)
        print(f"{cn:<12} {w:>3} {d:>3} {l:>3}  {at9:>5} {avg_t:>8.0f} {k:>4}/{lo:<4} {sh:>6} "
              f"{100 * hi / max(1, sh):>4.0f}% {str(mb):>8} {fb:>8}")
        losses = sorted(f"{r['opp']}:{r['side']}{r['rep']}({r.get('reason')}@{r.get('tick')})"
                        for r in rows if r["outcome"] != "W")
        if losses:
            print(f"   not won: {' '.join(losses)}")
    if args.baseline and args.baseline_file:
        res[args.baseline] = [json.loads(l) for l in open(args.baseline_file) if l.strip()]
        res[args.baseline] = [r for r in res[args.baseline] if r["cand"] == args.baseline]
    if args.baseline and res.get(args.baseline):
        base = {(r["opp"], r["side"], r["rep"]): r["outcome"] for r in res[args.baseline]}
        for cn in cands:
            if cn == args.baseline:
                continue
            worse = sorted(f"{o}:{s}{p}" for r in res[cn] for (o, s, p) in [(r["opp"], r["side"], r["rep"])]
                           if r["outcome"] != "W" and base.get((o, s, p)) == "W")
            better = sorted(f"{o}:{s}{p}" for r in res[cn] for (o, s, p) in [(r["opp"], r["side"], r["rep"])]
                            if r["outcome"] == "W" and base.get((o, s, p)) != "W")
            print(f"{cn} vs baseline {args.baseline}: newly lost {worse or '-'}; newly won {better or '-'}")


if __name__ == "__main__":
    main()

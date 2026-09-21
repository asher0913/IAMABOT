# Regression arena

Reproducible local matches with the real engine, so every tactical change is judged on the
same opponents from both sides before it becomes a default.

```sh
mm-cli run --quiet            # once: builds .mm/bin/mm-engine and the native library
python3 tools/arena/arena.py base=baseline_v12 "cand=live:IAMABOT_PUSH=1" \
    --opps quick --baseline base -j 8 --out tools/arena/results/my_test.jsonl
```

- `NAME=BOT[:K=V;K=V]`: `BOT` is a directory under `bots/` or `live` (this repo's `strategy/`
  as it is on disk); the `K=V` pairs are environment overrides (the strategy reads `IAMABOT_*`).
- `--opps key` (7 opponents, ~1 minute: the replicas of the teams that beat us plus the ones
  where past changes broke first), `quick` (13), `full` (all of `roster.py`),
  `tag:raid`, or a comma list. Every game is played with us as A and as B.
- `--baseline NAME` prints the games the candidate newly lost and newly won against that
  baseline; `--baseline-file` reuses the baseline rows of an earlier JSONL instead of replaying.
- Each game prints one line as it finishes (`| tee` / `tail -f` the log), then a summary:
  W/D/L, games that went to 9000, kills/losses, shots, hit rate, the lowest compute bank and
  the number of exception fallbacks (from `IAMABOT_STATS=1`, which the arena always sets).
- `--keep` keeps the gamelogs in `.work/logs`; `python3 tools/arena/replay.py table LOG`
  prints shots / hits / losses / heals per 1000 ticks for one of them.

Rule for changing the default: a change is adopted only if, on the full roster and both
sides, it loses no game the baseline won. Runs write their raw JSONL to `results/` (not kept in
the repository; the evidence behind each version is summarised in `docs/STRATEGY_zh.md`).

Opponents are built from our own controllers (`bots/styles_*`) with a different plan, so
their fire control is at least as good as ours: they overstate enemy micro and do not
reproduce every real opponent (the JaniceKeepTalking payload shield and the Gang v7 loss
still do not reproduce locally).

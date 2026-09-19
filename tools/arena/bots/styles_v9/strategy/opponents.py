"""Opponent styles built on the v9 core (same fire control, healing and production code as
the production bot); only the plan differs."""

import math
import os

from .brain import AdvancedStrategy


class Deathball(AdvancedStrategy):
    """Team Name / JaniceKeepTalking / clankerbot as seen on 2026-09-19 (matches 656, 841,
    907): three extractors, the whole army packed inside the capture circle from the
    first fight on, pushing and shooting whatever comes into range; healers glued to it.
    Nobody ever leaves the circle to chase."""

    OPENING = os.environ.get("BALL_OPENING", "EBBBHBHBEEBHBBHBB")
    EXTRACTOR_TARGET = int(os.environ.get("BALL_EXTRACTORS", "3"))
    HEALER_RATIO = float(os.environ.get("BALL_HEALERS", "0.33"))
    R_OUT = float(os.environ.get("BALL_R", "2.3"))
    SPACING = float(os.environ.get("BALL_GAP", "0.6"))

    def _ball_spots(self):
        px, py = self.P
        key = (round(px * 4), round(py * 4))
        if getattr(self, "_ball_key", None) == key:
            return self._ball
        ux, uy = self.u
        spots = []
        r = 1.15
        while r <= self.R_OUT + 1e-6:
            n = max(6, int(2 * math.pi * r / self.SPACING))
            for k in range(n):
                a = 2 * math.pi * k / n
                x, y = px + math.cos(a) * r, py + math.sin(a) * r
                if self._disc_free(x, y, 0.3):
                    # Inner rings first, then the side facing the enemy.
                    spots.append((r - 0.3 * (math.cos(a) * ux + math.sin(a) * uy), x, y))
            r += self.SPACING * 0.9
        spots.sort()
        self._ball_key, self._ball = key, [(x, y) for _, x, y in spots]
        return self._ball

    def _battle_moves(self, front, moves):
        if not front:
            return
        spots = self._ball_spots()
        if not spots:
            for b in front:
                moves[b.id] = self._nav(b.x, b.y, self.P[0], self.P[1])
            return
        spots = spots[: max(len(front), 1) + 4]
        pairs = sorted(
            ((b.x - sx) ** 2 + (b.y - sy) ** 2, b.id, k)
            for b in front
            for k, (sx, sy) in enumerate(spots)
        )
        tb, ts, owner = set(), set(), {}
        for d, bid, k in pairs:
            if bid in tb or k in ts:
                continue
            tb.add(bid)
            ts.add(k)
            owner[bid] = k
        for b in front:
            sx, sy = spots[owner[b.id]]
            if (b.x - sx) ** 2 + (b.y - sy) ** 2 < 0.05 ** 2:
                moves[b.id] = (0.0, 0.0)
            else:
                moves[b.id] = self._nav(b.x, b.y, sx, sy)

    def _separate(self, me, moves):
        return

    def _pick_guards(self, battles, op):
        self.raid = []
        return {}

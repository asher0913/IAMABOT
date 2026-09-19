"""Tournament entry point for IAMABOT.

The arena is mirrored by the engine, so the production bot deliberately uses the
same strategy on both sides.  ``MM_IAMABOT_LOCAL_AB`` only exists to make local
candidate-versus-reference testing convenient; the tournament never sets it.
"""

from __future__ import annotations

import os

from . import *
from .brain import AdvancedStrategy, ReferenceStrategy


def get_strategy(team: int) -> Strategy:
    matchup = os.environ.get("MM_IAMABOT_LOCAL_AB", "").lower()
    if matchup == "advanced-a":
        return AdvancedStrategy() if team == 0 else ReferenceStrategy()
    if matchup == "advanced-b":
        return ReferenceStrategy() if team == 0 else AdvancedStrategy()
    return AdvancedStrategy()

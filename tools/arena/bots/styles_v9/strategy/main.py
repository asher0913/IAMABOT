import os

from . import *
from .brain import AdvancedStrategy
from . import opponents


def get_strategy(team: int) -> Strategy:
    name = os.environ.get("OPP", "advanced").lower()
    table = {
        "advanced": AdvancedStrategy,
        "deathball": opponents.Deathball,
    }
    return table[name]()

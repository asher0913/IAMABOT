from . import *
from .brain import AdvancedStrategyNew


def get_strategy(team: int) -> Strategy:
    return AdvancedStrategyNew()

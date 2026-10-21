"""
Indicator registry.

Every indicator file in this package must expose:
    META      — dict with key, label, defaults, columns
    compute() — pure math:  DataFrame  →  dict of Series
    signals() — trigger logic:  (df, params, computed)  →  dict of values

To add a new indicator:
    1. create indicators/myind.py
    2. add its import below
    3. add it to REGISTRY
Nothing else in the app needs to change.
"""

from . import t3          # ← uncommented in Session B
from . import wavetrend   # ← uncommented in Session C
from . import chandelier  # ← uncommented in Session D
from . import support_resistance as st

REGISTRY = {
    "t3": t3,
    "wt": wavetrend,
    "ce": chandelier,
    "st": st,
}


def list_all():
    """Return {key: META} for the UI to build its checkboxes."""
    return {k: v.META for k, v in REGISTRY.items()}


def get(key):
    """Return the module for one indicator, or None."""
    return REGISTRY.get(key)
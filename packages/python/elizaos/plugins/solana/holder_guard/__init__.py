"""holder_guard — deterministic holder-distribution guard for pump.fun / PumpSwap.

Upstream of any LLM/classifier layer. Ships in log-only mode by default
(HOLDER_GUARD_ENFORCE=false). Flip to enforce once the "would have blocked"
logs show a tolerable false-positive rate.
"""
from .guard import HolderGuard
from .models import GuardDecision, HolderDelta, HolderSnapshot
from . import config

__all__ = [
    "HolderGuard",
    "GuardDecision",
    "HolderSnapshot",
    "HolderDelta",
    "config",
]

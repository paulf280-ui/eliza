"""holder_guard.config — all thresholds in one place.

Every value here is data-driven: observed reference tokens (AINI, SAM, LARP)
pass the sweet_spot gate; TRADE triggers a hard_block on top-10.

Two global kill-switches:
  HOLDER_GUARD_ENABLED — module is live if False everything no-ops (True by default).
  HOLDER_GUARD_ENFORCE — False = log-only ("would have blocked X"). True = actually veto
                         entries and fire holder-flow exits. Default False until we see
                         false-positive rate in live logs.
"""
from __future__ import annotations

import os


def _env_on(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


HOLDER_GUARD_ENABLED = _env_on("HOLDER_GUARD_ENABLED", "true")
HOLDER_GUARD_ENFORCE = _env_on("HOLDER_GUARD_ENFORCE", "false")


# ── Hard-block thresholds (non-negotiable vetoes) ───────────────────────
TOP_10_MAX_PCT                = 35.0
DEV_HOLDING_MAX_PCT           = 5.0
REQUIRE_LP_BURNED             = True    # True = block if LP not ≥99% burned
REQUIRE_MINT_RENOUNCED        = True    # block if mint authority not null
REQUIRE_FREEZE_RENOUNCED      = True    # block if freeze authority not null

# These would require Bubblemaps / Nansen / audit-feed subscriptions.
# Left as thresholds so future-us can plug in data without changing callers.
SNIPERS_HOLDING_MAX_PCT       = 10.0    # applied only when snapshot.snipers_pct is not None
BUNDLERS_HOLDING_MAX_PCT      = 5.0     # applied only when snapshot.bundlers_pct is not None
AUDIT_SCORE_MIN               = 6       # applied only when snapshot.audit_score is not None
FRESH_WALLETS_IN_TOP_20_MAX_PCT = 25.0  # applied only when snapshot.fresh_pct is not None


# ── Sweet-spot acceptance gate ──────────────────────────────────────────
TOP_10_MIN_PCT_SWEET          = 5.0
TOP_10_MAX_PCT_SWEET          = 25.0
DEV_HOLDING_MAX_PCT_SWEET     = 1.0
MIN_UNIQUE_HOLDERS            = 500
MIN_HOLDER_GROWTH_PER_MIN_PCT = 5.0     # holders/min as % of current holder count
REQUIRE_BUYS_OUTPACE_SELLS    = True

# Optional fields — enforced only when data is present (same as hard-block optionals)
SNIPERS_HOLDING_MAX_PCT_SWEET = 2.0
BUNDLERS_HOLDING_MAX_PCT_SWEET = 1.0
AUDIT_SCORE_MIN_SWEET         = 7


# ── Flow-monitor thresholds (open-position exit signals) ────────────────
FLOW_POLL_INTERVAL_SECS       = 45
FLOW_DELTA_WINDOWS_MINS       = (1, 5, 15)

# Hard exit triggers
TOP_10_JUMP_ABS_PCT_OVER_5MIN = 3.0     # single whale soaking supply
HOLDER_COUNT_DROP_PCT_10MIN   = 5.0     # mass exit — distribution top
CEX_MOVE_AUTO_EXIT            = True    # dev/sniper to CEX = panic exit

# Accumulation signals (suppress price-based exit when these are true)
HOLDER_GROWTH_HOLD_OVERRIDE_PER_MIN_PCT = 2.0   # still-growing holder count overrides short-term price noise
TOP_10_DECREASING_HOLD_DELTA_PCT        = -1.0  # top-10 dropping = distribution TO retail = hold


# ── CEX registry refresh ────────────────────────────────────────────────
CEX_REGISTRY_REFRESH_SECS     = 7 * 24 * 3600   # weekly
CEX_WALLETS_TO_MONITOR        = 5               # top-N + dev wallet per active position

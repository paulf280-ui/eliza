"""Developer wallet reputation — blacklist/whitelist for pump.fun token creators.

Tracks outcomes per deployer wallet so the bot can:
  - Skip tokens from known ruggers (blacklist)
  - Prioritise tokens from proven profitable devs (whitelist)
  - Auto-blacklist wallets after 2+ confirmed rug pulls
  - PROVEN tier: creator with 2+ successful token launches, 0 rugs → 75% wallet allocation

Storage: JSON file alongside this module (dev_lists.json).
Thread-safe for asyncio (single-process, GIL protected dict ops).

Tiers:
  proven     — 2+ successful launches, 0 rugs → go 75% wallet on their next token
  whitelisted — 1+ wins, 0 rugs → use normal strategy with priority
  unknown    — no data yet
  blacklisted — 2+ rugs → hard skip
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

DEV_LISTS_FILE = os.path.join(os.path.dirname(__file__), "dev_lists.json")

ReputationStatus = Literal["blacklisted", "whitelisted", "proven", "unknown"]
OutcomeType = Literal["win", "loss", "rug"]

# A "rug" is a stop-loss that fires within 120 seconds of entry AND drops > 50%
# beyond the stated SL (i.e. gapped through SL by at least 2×)
RUG_LOSS_THRESHOLD = -0.50    # pnl_pct below this AND hold_secs < 120 → rug
RUG_TIME_THRESHOLD  = 120     # seconds — fast dump = suspicious

# Auto-blacklist after this many confirmed rugs from a single wallet
AUTO_BLACKLIST_AFTER_RUGS = 2

# Auto-promote to PROVEN after this many successful launches with zero rugs
PROVEN_AFTER_WINS = 2


@dataclass
class DevRecord:
    wallet: str
    status: ReputationStatus = "unknown"
    reason: str = ""
    added_at: float = field(default_factory=time.time)
    tokens_launched: list[str] = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    rugs: int = 0
    successful_launches: int = 0   # wins from pump.fun / pumpswap dex (BC + graduation)
    last_seen: float = field(default_factory=time.time)

    @property
    def total_trades(self) -> int:
        return self.wins + self.losses + self.rugs

    @property
    def rug_rate(self) -> float:
        return self.rugs / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def is_proven(self) -> bool:
        return self.status == "proven"

    @property
    def is_trusted(self) -> bool:
        """True for both whitelist and proven tiers."""
        return self.status in ("whitelisted", "proven")


class DevReputation:
    """Manages developer wallet blacklist/whitelist with persistent JSON storage."""

    def __init__(self, filepath: str = DEV_LISTS_FILE) -> None:
        self._filepath = filepath
        self._records: dict[str, DevRecord] = {}
        self._load()

    # ──────────────────────────── persistence ────────────────────────────────

    def _load(self) -> None:
        try:
            with open(self._filepath) as f:
                raw = json.load(f)

            # Handle old format: {"blacklisted": {}, "whitelisted": {}}
            # New format: {"wallet_addr": {record_dict}, ...}
            if set(raw.keys()).issubset({"blacklisted", "whitelisted"}):
                # Old format — nothing useful to load
                return

            allowed = set(DevRecord.__dataclass_fields__)
            for wallet, rec_dict in raw.items():
                if not isinstance(rec_dict, dict):
                    continue
                cleaned = {k: v for k, v in rec_dict.items() if k in allowed}
                if "wallet" not in cleaned:
                    cleaned["wallet"] = wallet
                try:
                    self._records[wallet] = DevRecord(**cleaned)
                except TypeError:
                    pass  # Field mismatch on old record — skip
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass  # Fresh start — no file yet

    def _save(self) -> None:
        try:
            with open(self._filepath, "w") as f:
                json.dump(
                    {w: asdict(r) for w, r in self._records.items()},
                    f,
                    indent=2,
                )
        except Exception:
            pass  # Non-fatal

    # ──────────────────────────── public API ─────────────────────────────────

    def check(self, wallet: str) -> tuple[ReputationStatus, str]:
        """Return (status, reason). Status is 'blacklisted'|'whitelisted'|'proven'|'unknown'."""
        if not wallet:
            return "unknown", ""
        rec = self._records.get(wallet)
        if rec is None:
            return "unknown", ""
        return rec.status, rec.reason

    def is_blacklisted(self, wallet: str) -> bool:
        status, _ = self.check(wallet)
        return status == "blacklisted"

    def is_whitelisted(self, wallet: str) -> bool:
        status, _ = self.check(wallet)
        return status in ("whitelisted", "proven")

    def is_proven(self, wallet: str) -> bool:
        status, _ = self.check(wallet)
        return status == "proven"

    def get_proven_creators(self) -> list[str]:
        """Return list of wallet addresses with PROVEN status."""
        return [w for w, r in self._records.items() if r.status == "proven"]

    def get_whitelisted_creators(self) -> list[str]:
        """Return list of wallet addresses with WHITELISTED or PROVEN status."""
        return [w for w, r in self._records.items() if r.status in ("whitelisted", "proven")]

    def blacklist(self, wallet: str, reason: str = "manual") -> None:
        """Manually blacklist a wallet."""
        rec = self._records.get(wallet) or DevRecord(wallet=wallet)
        rec.status = "blacklisted"
        rec.reason = reason
        rec.last_seen = time.time()
        self._records[wallet] = rec
        self._save()
        print(f"[dev-rep] BLACKLISTED {wallet[:16]}... — {reason}")

    def whitelist(self, wallet: str, reason: str = "manual") -> None:
        """Manually whitelist a wallet (does not override proven)."""
        rec = self._records.get(wallet) or DevRecord(wallet=wallet)
        if rec.status != "proven":  # don't downgrade proven → whitelisted
            rec.status = "whitelisted"
        rec.reason = reason
        rec.last_seen = time.time()
        self._records[wallet] = rec
        self._save()
        print(f"[dev-rep] WHITELISTED {wallet[:16]}... — {reason}")

    def promote_to_proven(self, wallet: str, reason: str = "manual") -> None:
        """Manually or auto-promote a creator to PROVEN tier (2+ successful launches)."""
        rec = self._records.get(wallet) or DevRecord(wallet=wallet)
        rec.status = "proven"
        rec.reason = reason
        rec.last_seen = time.time()
        self._records[wallet] = rec
        self._save()
        print(f"[dev-rep] ⭐ PROVEN {wallet[:16]}... — {reason}")

    def remove(self, wallet: str) -> None:
        """Remove a wallet from tracking entirely."""
        self._records.pop(wallet, None)
        self._save()

    def record_outcome(
        self,
        wallet: str,
        mint: str,
        outcome: OutcomeType,
        pnl_pct: float = 0.0,
        hold_secs: float = 0.0,
        dex: str = "",
    ) -> None:
        """Record a trade outcome for a developer wallet.

        Auto-blacklists after AUTO_BLACKLIST_AFTER_RUGS confirmed rug pulls.
        Auto-promotes to PROVEN after PROVEN_AFTER_WINS pump.fun/pumpswap wins with 0 rugs.
        """
        if not wallet:
            return
        rec = self._records.get(wallet) or DevRecord(wallet=wallet)
        if mint and mint not in rec.tokens_launched:
            rec.tokens_launched.append(mint)
        rec.last_seen = time.time()

        if outcome == "win":
            rec.wins += 1
            # Count pump.fun / pumpswap wins as successful launches (BC snipe + graduation snipe)
            if dex in ("pump_fun", "pumpswap", ""):
                rec.successful_launches += 1
        elif outcome == "loss":
            rec.losses += 1
        elif outcome == "rug":
            rec.rugs += 1
            print(
                f"[dev-rep] RUG recorded: {wallet[:16]}... "
                f"mint={mint[:12]}... pnl={pnl_pct:.0f}% in {hold_secs:.0f}s "
                f"(total rugs: {rec.rugs})"
            )
            if rec.rugs >= AUTO_BLACKLIST_AFTER_RUGS and rec.status not in ("blacklisted", "proven"):
                rec.status = "blacklisted"
                rec.reason = (
                    f"Auto-blacklisted: {rec.rugs} confirmed rug pulls "
                    f"(rug rate {rec.rug_rate*100:.0f}%)"
                )
                print(f"[dev-rep] AUTO-BLACKLISTED {wallet[:16]}...")

        # Auto-promote to PROVEN: 2+ successful pump.fun/pumpswap launches, 0 rugs
        if (
            rec.successful_launches >= PROVEN_AFTER_WINS
            and rec.rugs == 0
            and rec.status not in ("proven", "blacklisted")
        ):
            rec.status = "proven"
            rec.reason = (
                f"Auto-proven: {rec.successful_launches} successful pump.fun/pumpswap launches, "
                f"0 rugs, {rec.wins} wins"
            )
            print(f"[dev-rep] ⭐ AUTO-PROVEN {wallet[:16]}... — {rec.reason}")

        # Auto-whitelist after consistent wins with no rugs (lower threshold than proven)
        elif rec.wins >= 1 and rec.rugs == 0 and rec.status == "unknown":
            rec.status = "whitelisted"
            rec.reason = f"Auto-whitelisted: {rec.wins} win(s), 0 rugs"
            print(f"[dev-rep] AUTO-WHITELISTED {wallet[:16]}...")

        self._records[wallet] = rec
        self._save()

    def classify_outcome(
        self, pnl_pct: float, hold_secs: float, reason: str
    ) -> OutcomeType:
        """Classify a trade result as 'win', 'loss', or 'rug' based on metrics."""
        if pnl_pct <= RUG_LOSS_THRESHOLD and hold_secs <= RUG_TIME_THRESHOLD:
            return "rug"
        if pnl_pct > 0:
            return "win"
        return "loss"

    def get_stats(self) -> dict:
        return {
            "blacklisted": sum(1 for r in self._records.values() if r.status == "blacklisted"),
            "whitelisted": sum(1 for r in self._records.values() if r.status == "whitelisted"),
            "proven": sum(1 for r in self._records.values() if r.status == "proven"),
            "tracked": len(self._records),
            "total_rugs_recorded": sum(r.rugs for r in self._records.values()),
        }

    def get_all_records(self, status_filter: str | None = None) -> list[dict]:
        """Return all records sorted by status tier then win count."""
        _tier_order = {"proven": 0, "whitelisted": 1, "unknown": 2, "blacklisted": 3}
        records = list(self._records.values())
        if status_filter:
            records = [r for r in records if r.status == status_filter]
        records.sort(key=lambda r: (_tier_order.get(r.status, 9), -r.wins, r.rugs))
        return [asdict(r) for r in records]

    def get_record(self, wallet: str) -> dict | None:
        """Return a single record as dict, or None if not tracked."""
        rec = self._records.get(wallet)
        return asdict(rec) if rec else None


# Module-level singleton — import and use directly
_reputation: DevReputation | None = None


def get_reputation() -> DevReputation:
    """Return the module-level singleton, creating it on first call."""
    global _reputation
    if _reputation is None:
        _reputation = DevReputation()
    return _reputation

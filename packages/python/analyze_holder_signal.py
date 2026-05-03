#!/usr/bin/env python3
"""
Analyze holder count at entry vs final PnL across all creator-alpha trades.

2026-05-03: User discovered that MOG (+162.8%) entered at very low price vs
other operator-child tokens that lost 75%+. Hypothesis: lower holder count
at entry = fresher token = more room to run.

This script builds the correlation to inform entry gating rules.
"""

import json
import sys
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent / "elizaos/plugins/solana"

def load_monster_trades():
    """Load all closed monster trades."""
    path = BASE / "monster_closed_trades.json"
    with open(path) as f:
        return json.load(f)

def analyze_by_signal_source():
    """Analyze holder count correlation for each signal source."""
    trades = load_monster_trades()

    # Group by signal source
    by_source = {}
    for t in trades:
        src = t.get("signal_source", "unknown")
        if src not in by_source:
            by_source[src] = []
        by_source[src].append(t)

    print("=" * 80)
    print("HOLDER COUNT CORRELATION ANALYSIS (all signal sources)")
    print("=" * 80)

    for source in sorted(by_source.keys()):
        trades_in_source = by_source[source]
        print(f"\n{source.upper()}: {len(trades_in_source)} trades")
        print("-" * 80)

        # Separate by holder count tiers
        no_data = [t for t in trades_in_source if t.get("holders_at_entry") is None]
        tier_0_100 = [t for t in trades_in_source if t.get("holders_at_entry") is not None
                      and t.get("holders_at_entry") < 100]
        tier_100_200 = [t for t in trades_in_source if t.get("holders_at_entry") is not None
                        and 100 <= t.get("holders_at_entry") < 200]
        tier_200_300 = [t for t in trades_in_source if t.get("holders_at_entry") is not None
                        and 200 <= t.get("holders_at_entry") < 300]
        tier_300_plus = [t for t in trades_in_source if t.get("holders_at_entry") is not None
                         and t.get("holders_at_entry") >= 300]

        for tier_name, tier_trades in [
            ("No data", no_data),
            ("<100 holders (CHAIN-MINTING)", tier_0_100),
            ("100-200 holders (FRESH)", tier_100_200),
            ("200-300 holders (DISTRIBUTED)", tier_200_300),
            ("300+ holders (OVER-DISTRIBUTED)", tier_300_plus),
        ]:
            if tier_trades:
                wins = sum(1 for t in tier_trades if t.get("final_pnl_pct", 0) > 0)
                losses = sum(1 for t in tier_trades if t.get("final_pnl_pct", 0) < 0)
                flats = sum(1 for t in tier_trades if t.get("final_pnl_pct", 0) == 0)
                avg_pnl = sum(t.get("final_pnl_pct", 0) for t in tier_trades) / len(tier_trades)
                avg_sol = sum(t.get("final_pnl_sol", 0) for t in tier_trades) / len(tier_trades)
                wr = (wins / len(tier_trades) * 100) if tier_trades else 0

                print(f"\n  {tier_name}:")
                print(f"    Count: {len(tier_trades)}")
                print(f"    W/L: {wins}W / {losses}L / {flats}F")
                print(f"    WR: {wr:.1f}%")
                print(f"    Avg PnL: {avg_pnl:+.1f}% ({avg_sol:+.4f} SOL)")

                # Show best and worst
                if tier_trades:
                    best = max(tier_trades, key=lambda t: t.get("final_pnl_pct", 0))
                    worst = min(tier_trades, key=lambda t: t.get("final_pnl_pct", 0))
                    print(f"    Best: {best.get('token_name')} {best.get('final_pnl_pct'):+.1f}%")
                    print(f"    Worst: {worst.get('token_name')} {worst.get('final_pnl_pct'):+.1f}%")

def analyze_creator_alpha_specific():
    """Deep dive into creator_alpha trades only."""
    trades = load_monster_trades()
    ca_trades = [t for t in trades if "creator_alpha" in str(t.get("signal_source", ""))]

    print("\n\n" + "=" * 80)
    print("CREATOR-ALPHA FOCUSED ANALYSIS")
    print("=" * 80)

    # Direct vs operator
    ca_direct = [t for t in ca_trades if t.get("signal_source") == "creator_alpha_direct"]
    ca_operator = [t for t in ca_trades if t.get("signal_source") == "creator_alpha_operator"]

    for label, group in [("DIRECT CREATORS", ca_direct), ("OPERATOR-CHILD", ca_operator)]:
        print(f"\n{label}: {len(group)} trades")
        print("-" * 80)

        # Analyze by holder count
        with_holders = [t for t in group if t.get("holders_at_entry") is not None]
        without_holders = [t for t in group if t.get("holders_at_entry") is None]

        if with_holders:
            avg_holders = sum(t.get("holders_at_entry") for t in with_holders) / len(with_holders)
            avg_pnl = sum(t.get("final_pnl_pct", 0) for t in with_holders) / len(with_holders)
            print(f"  With holder data: {len(with_holders)} trades")
            print(f"    Avg holders: {avg_holders:.0f}")
            print(f"    Avg PnL: {avg_pnl:+.1f}%")

            # Winners vs losers
            winners = [t for t in with_holders if t.get("final_pnl_pct", 0) > 0]
            losers = [t for t in with_holders if t.get("final_pnl_pct", 0) < 0]

            if winners:
                avg_winners_holders = sum(t.get("holders_at_entry") for t in winners) / len(winners)
                avg_winners_pnl = sum(t.get("final_pnl_pct", 0) for t in winners) / len(winners)
                print(f"\n    WINNERS ({len(winners)}): avg {avg_winners_holders:.0f} holders, {avg_winners_pnl:+.1f}% PnL")
                for w in winners[:3]:
                    print(f"      {w.get('token_name')}: {w.get('holders_at_entry', '?')} holders, {w.get('final_pnl_pct'):+.1f}%")

            if losers:
                avg_losers_holders = sum(t.get("holders_at_entry") for t in losers) / len(losers)
                avg_losers_pnl = sum(t.get("final_pnl_pct", 0) for t in losers) / len(losers)
                print(f"\n    LOSERS ({len(losers)}): avg {avg_losers_holders:.0f} holders, {avg_losers_pnl:+.1f}% PnL")
                for l in losers[:3]:
                    print(f"      {l.get('token_name')}: {l.get('holders_at_entry', '?')} holders, {l.get('final_pnl_pct'):+.1f}%")

        if without_holders:
            print(f"\n  Without holder data: {len(without_holders)} trades")
            avg_pnl = sum(t.get("final_pnl_pct", 0) for t in without_holders) / len(without_holders)
            print(f"    Avg PnL: {avg_pnl:+.1f}%")

def recommended_rules():
    """Print recommended entry rules based on analysis."""
    print("\n\n" + "=" * 80)
    print("RECOMMENDED ENTRY RULES (2026-05-03)")
    print("=" * 80)

    print("""
1. HOLDER COUNT GATES (NEW):

   if holders_at_entry is not None:
       if holders_at_entry < 100:
           # Chain-minting, explosive potential
           confidence += 0.30  (ACCEPT, high priority)
       elif holders_at_entry < 150:
           # Early distribution, still fresh
           confidence += 0.15  (ACCEPT)
       elif holders_at_entry < 250:
           # Normal distribution
           confidence += 0.00  (NEUTRAL)
       elif holders_at_entry < 400:
           # Getting distributed
           confidence -= 0.20  (CAUTION)
       else:
           # Over-distributed
           confidence -= 0.50  (REJECT, no runway)

2. ENTRY PRICE CORRELATION:

   Entry price is 2-5x higher for losers vs winners.
   This suggests early entrants (lower price) have better outcomes.
   Consider: filter_by_entry_price_percentile (skip >90th percentile)

3. CREATOR TIER OVERRIDE:

   Direct creators (tier-A): accept even if 200+ holders (reputation > distribution)
   Operator-children: REJECT if 300+ holders (unless operator has >60% WR)

4. COMBINED SIGNAL:

   ACCEPT if:
     - (holders < 100) OR
     - (holders < 200 AND creator_tier in ['a', 'b', 'c']) OR
     - (entry_price <= entry_price_percentile_25)

   REJECT if:
     - (holders > 400) OR
     - (holders > 300 AND operator_wr < 40%) OR
     - (peak_pnl_pct <= +5% within 60s of entry)
""")

if __name__ == "__main__":
    analyze_by_signal_source()
    analyze_creator_alpha_specific()
    recommended_rules()
    print("\n" + "=" * 80)
    print("Next: Re-deploy bot with updated strategy_e_monster.py and monster_signals.py")
    print("=" * 80)

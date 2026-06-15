"""alpha_analyze.py — score wallets from the PRIVATE accumulated store.

Run this after the accumulator (alpha_engine.py) has gathered a few weeks of
data. It answers: do specific wallets recur in the early holders of tokens that
GRADUATED (and ran), more than the base rate would predict?

Reads alpha_engine.db only. Writes nothing. Prints a ranked smart-money list +
the honest base-rate lift so we know whether the edge is real before trusting it.
"""
from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
ALPHA_DB = os.environ.get("ALPHA_DB", str(BASE / "alpha_engine.db"))

# A wallet is only "smart money" if it has enough samples to beat luck.
MIN_TOKENS = int(os.environ.get("ALPHA_MIN_TOKENS", "3"))
# "WIN" = graduated, OR peak multiple >= this over our first-seen baseline.
WIN_PEAK_MULT = float(os.environ.get("ALPHA_WIN_PEAK", "2.0"))


def main() -> None:
    db = sqlite3.connect(f"file:{ALPHA_DB}?mode=ro", uri=True)
    c = db.cursor()

    tokens = c.execute(
        "SELECT t.mint, COALESCE(o.graduated,0), COALESCE(o.peak_multiple,0), "
        "COALESCE(o.status,'NONE') FROM tokens t LEFT JOIN outcomes o ON o.mint=t.mint"
    ).fetchall()
    if not tokens:
        print("no tokens yet — let the accumulator run first")
        return

    def is_win(grad, peak):
        return bool(grad) or (peak >= WIN_PEAK_MULT)

    outcome = {m: is_win(g, p) for m, g, p, _ in tokens}
    n = len(tokens)
    wins = sum(outcome.values())
    base = wins / n if n else 0
    grad_n = sum(1 for _, g, _, _ in tokens if g)
    print(f"tokens tracked: {n}")
    print(f"graduated: {grad_n}  |  wins (grad or >={WIN_PEAK_MULT}x): {wins}  "
          f"|  base win rate: {base:.1%}")

    # wallet -> the tokens it was early on (exclude CEX/infra-labelled rows)
    wallet_tokens: dict[str, list[str]] = defaultdict(list)
    for mint, wallet in c.execute(
        "SELECT mint, wallet FROM holdings WHERE label IS NULL"
    ).fetchall():
        if mint in outcome:
            wallet_tokens[wallet].append(mint)

    qualified = {w: ts for w, ts in wallet_tokens.items() if len(set(ts)) >= MIN_TOKENS}
    print(f"\nwallets on >={MIN_TOKENS} tokens: {len(qualified)} "
          f"(of {len(wallet_tokens)} total)")

    scored = []
    for w, ts in qualified.items():
        ms = set(ts)
        w_wins = sum(1 for m in ms if outcome[m])
        rate = w_wins / len(ms)
        # binomial lift vs base
        lift = (rate / base) if base else 0
        scored.append((w, len(ms), w_wins, rate, lift))

    # aggregate honesty check: across all qualified wallets, are their picks
    # better than random?
    tot = sum(len(set(ts)) for ts in qualified.values())
    hit = sum(sum(1 for m in set(ts) if outcome[m]) for ts in qualified.values())
    if tot:
        print(f"qualified-wallet picks that won: {hit}/{tot} = {hit/tot:.1%} "
              f"(vs base {base:.1%}) -> LIFT {((hit/tot)/base):.2f}x" if base else "")

    scored.sort(key=lambda x: (-x[4], -x[1]))
    print(f"\n=== TOP SMART-MONEY CANDIDATES (>={MIN_TOKENS} tokens, ranked by lift) ===")
    for w, ntok, nw, rate, lift in scored[:40]:
        print(f"  {w[:6]}..{w[-4:]}  {nw}/{ntok} won  ({rate:.0%})  lift {lift:.2f}x")
    print(f"\ntotal candidates: {len(scored)}")
    print("\nNOTE: trust this only when base rate is computed over enough tokens and "
          "candidates have >=4-5 samples — small-n 100%% wallets are mostly luck.")
    db.close()


if __name__ == "__main__":
    main()

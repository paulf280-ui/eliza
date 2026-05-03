# Trading Journal

This folder is Jarvis's memory of every trade — paper and live.

## File naming
- `YYYY-MM-DD_paper.json`  — paper trades for that day
- `YYYY-MM-DD_live.json`   — live trades for that day
- `analysis_notes.json`    — Jarvis's own written analysis (updated by Jarvis)
- `bad_token_dna.json`     — patterns extracted from losing trades (updated by Jarvis)

## What each trade record contains
Entry DNA: symbol, dex, liq at entry, vol/liq at entry, age at entry (hours),
h1% at entry, m5% at entry, buy ratio at entry, has_socials, score, score_reasons.
Exit: pnl_pct, pnl_sol, reason, hold_secs, peak_pnl_pct, outcome (win/loss/rug).

## Purpose
Jarvis reads these files every cycle so he can:
1. Learn what bad entries look like (h1 >100% = already ran, m5 >20% = chasing)
2. Recognise monster entry patterns (buy_ratio 50-65%, age 2-20h, quiet m5)
3. Write his own analysis notes that survive restarts
4. Build a database of bad token DNA to block re-entry patterns

"""Report the shadow-sim results: overall + per-operator +75% hit rate."""
import sqlite3, time
from pathlib import Path
db = sqlite3.connect(f"file:{Path(__file__).parent/'shadow_sim.db'}?mode=ro", uri=True)
c = db.cursor()
g = lambda q: c.execute(q).fetchone()
start = (c.execute("SELECT v FROM sim_meta WHERE k='start'").fetchone() or [time.time()])[0]
hrs = (time.time() - start) / 3600
print(f"==== SHADOW COPY-TRADE SIM — {hrs:.1f}h elapsed ====\n")
tot = g("SELECT COUNT(*) FROM shadow_trades")[0]
print(f"shadow trades taken: {tot}")
if not tot:
    print("(no launches detected yet — operators haven't fired since sim start)"); raise SystemExit
for st, n in c.execute("SELECT status, COUNT(*) FROM shadow_trades GROUP BY status ORDER BY 2 DESC"):
    print(f"  {st}: {n}")
closed = g("SELECT COUNT(*) FROM shadow_trades WHERE status IN ('WIN','RUG','TIMEOUT')")[0]
wins = g("SELECT COUNT(*) FROM shadow_trades WHERE status='WIN'")[0]
print(f"\n+75% TP HIT RATE: {wins}/{closed} = {(100*wins/closed) if closed else 0:.0f}%  (of resolved trades)")
ttp = g("SELECT AVG(time_to_tp_s), MIN(time_to_tp_s), MAX(time_to_tp_s) FROM shadow_trades WHERE status='WIN'")
if ttp and ttp[0]:
    print(f"time to +75%: avg {ttp[0]/60:.1f}m  (min {ttp[1]/60:.1f}m, max {ttp[2]/60:.1f}m)")
lag = g("SELECT AVG(detect_lag_s) FROM shadow_trades")
print(f"avg detection lag (launch->our entry): {lag[0]:.0f}s" if lag and lag[0] else "")
print("\n=== PER-OPERATOR hit rate (resolved trades) ===")
for op, role, t, w in c.execute("""SELECT operator, role, COUNT(*) t,
        SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END) w
        FROM shadow_trades WHERE status IN ('WIN','RUG','TIMEOUT')
        GROUP BY operator ORDER BY w DESC, t DESC"""):
    print(f"  {op[:6]}..{op[-4:]} ({role}): {w}/{t} hit +75%  ({(100*w/t) if t else 0:.0f}%)")
print("\n=== peak distribution (what we'd have left on the table / lost) ===")
for lbl, lo, hi in [("hit 2x+", 2.0, 99), ("1.75-2x", 1.75, 2.0), ("1.3-1.75x", 1.3, 1.75), ("1-1.3x", 1.0, 1.3), ("dumped <1x", 0, 1.0)]:
    n = g(f"SELECT COUNT(*) FROM shadow_trades WHERE peak_mult>={lo} AND peak_mult<{hi} AND status!='open'")[0]
    print(f"  peak {lbl}: {n}")
db.close()

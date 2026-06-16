"""shadow_sim.py — PAPER copy-trade simulator (Phase 1, no capital).

Watches the LIVE operator watchlist (CEX-filtered, non-dormant). When a watchlist
operator launches a token, records a shadow trade and tracks whether it would have
hit the +75% TP before rugging. Pure data — measures the per-operator hit rate so
we know which operators are worth copying for real before risking a cent.

Detection:
  * DEPLOYER launches — watchlist deployer creates a token (Helius CREATE events).
  * FUNDER-funded launches — watchlist funder sends SOL to a fresh wallet that
    then creates a token within FUNDER_WINDOW (the rotated-deployer pattern).

Outcome (peak-based — a +75% limit order fires the instant price touches +75%):
  WIN     = peak >= entry × 1.75
  RUG     = price <= entry × 0.40 before reaching +75%
  TIMEOUT = neither within HOLD_CAP hours
"""
from __future__ import annotations
import asyncio, aiohttp, json, os, re, sqlite3, time, logging
from pathlib import Path

BASE = Path(__file__).parent
SIM_DB = str(BASE / "shadow_sim.db")
ALPHA_DB = str(BASE / "alpha_engine.db")
PRICE_URL = "http://127.0.0.1:3001/api/live-price"

TP_MULT      = float(os.environ.get("SHADOW_TP", "1.75"))   # +75%
RUG_MULT     = 0.40                                          # -60% before TP = rugged
HOLD_CAP_H   = 2.0                                           # close if no TP/rug in 2h
DETECT_EVERY = 20                                            # seconds between detect sweeps
FUNDER_WINDOW = 30 * 60                                      # funder→deploy must be <30 min
DORMANT_DAYS = 30

log = logging.getLogger("shadow_sim")
_HKEY = ""

# Quote tokens that appear in a launch's tokenTransfers as PAYMENT, not the new
# token — must be filtered out so we enter the actual launched mint.
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",   # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
}

def _new_mint(tx) -> str | None:
    """The actual launched token from a CREATE tx (skip quote/payment mints)."""
    for tt in tx.get("tokenTransfers") or []:
        m = tt.get("mint")
        if m and m not in QUOTE_MINTS:
            return m
    return None

def _resolve_keys():
    global _HKEY
    url = None
    for f in [BASE / ".env", Path("/home/ubuntu/eliza/.env")]:
        try:
            for line in open(f):
                m = re.search(r"https://[^\s\"']*helius[^\s\"']*", line)
                if m: url = m.group(0); break
        except Exception: pass
        if url: break
    os.environ.setdefault("SOLANA_RPC_URL", url or "")
    mk = re.search(r"api-key=([A-Za-z0-9_-]+)", url or "")
    if mk:
        _HKEY = mk.group(1)
    if not _HKEY:
        for f in [BASE / ".env", Path("/home/ubuntu/eliza/.env")]:
            try:
                for line in open(f):
                    m = re.match(r"\s*HELIUS_API_KEY\s*=\s*[\"']?([A-Za-z0-9_-]+)", line)
                    if m: _HKEY = m.group(1); break
            except Exception: pass
            if _HKEY: break

def _db():
    db = sqlite3.connect(SIM_DB, timeout=30)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            mint TEXT PRIMARY KEY, operator TEXT, role TEXT,
            entry_ts REAL, entry_price REAL, peak_price REAL, peak_mult REAL,
            status TEXT, exit_ts REAL, time_to_tp_s REAL, detect_lag_s REAL
        );
        CREATE TABLE IF NOT EXISTS seen_mints (mint TEXT PRIMARY KEY, ts REAL);
        CREATE TABLE IF NOT EXISTS funder_candidates (wallet TEXT PRIMARY KEY, funder TEXT, since REAL);
        CREATE TABLE IF NOT EXISTS sim_meta (k TEXT PRIMARY KEY, v REAL);
    """)
    db.commit()
    return db

def watchlist(db_alpha) -> dict:
    """Live operators: deployers + funders, non-infra, active < DORMANT_DAYS."""
    now = time.time()
    cls = {r[0]: (r[1], r[2]) for r in db_alpha.execute("SELECT wallet, klass, last_active FROM wallet_class").fetchall()}
    def live(w):
        c = cls.get(w)
        if not c: return True                      # unclassified — include
        if c[0] == "infra": return False
        if c[1] and now - c[1] > DORMANT_DAYS * 86400: return False
        return True
    wl = {}
    for w, n in db_alpha.execute("SELECT deployer, COUNT(DISTINCT mint) FROM token_operators GROUP BY deployer HAVING COUNT(DISTINCT mint)>=2"):
        if w and live(w): wl[w] = "deployer"
    for w, n in db_alpha.execute("SELECT deployer_funder, COUNT(DISTINCT mint) FROM token_operators WHERE deployer_funder IS NOT NULL GROUP BY deployer_funder HAVING COUNT(DISTINCT mint)>=2"):
        if w and live(w): wl.setdefault(w, "funder")
    return wl

async def helius(session, addr, typ=None, limit=15):
    if not _HKEY: return []
    url = f"https://api.helius.xyz/v0/addresses/{addr}/transactions?api-key={_HKEY}&limit={limit}"
    if typ: url += f"&type={typ}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
            return await r.json() if r.status == 200 else []
    except Exception:
        return []

async def price_of(session, mint):
    try:
        async with session.get(f"{PRICE_URL}?mint={mint}", timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json()
            return float(d.get("price_sol") or 0)
    except Exception:
        return 0.0

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [shadow] %(levelname)s %(message)s")
    _resolve_keys()
    db = _db()
    a = sqlite3.connect(f"file:{ALPHA_DB}?mode=ro", uri=True)
    start = db.execute("SELECT v FROM sim_meta WHERE k='start'").fetchone()
    if not start:
        db.execute("INSERT INTO sim_meta VALUES ('start', ?)", (time.time(),))
        db.commit()
        sim_start = time.time()
    else:
        sim_start = start[0]
    log.info("shadow sim — TP=%.0f%% hold_cap=%.1fh helius=%s", (TP_MULT-1)*100, HOLD_CAP_H, bool(_HKEY))

    wl = watchlist(a)
    wl_ts = time.time()
    log.info("watching %d live operators (%d deployers, %d funders)",
             len(wl), sum(1 for v in wl.values() if v=='deployer'), sum(1 for v in wl.values() if v=='funder'))
    deployers = [w for w, r in wl.items() if r == "deployer"]
    funders = [w for w, r in wl.items() if r == "funder"]
    di = fi = 0

    seen = {r[0] for r in db.execute("SELECT mint FROM seen_mints").fetchall()}
    # No upfront baseline needed — the watch loop only enters launches whose CREATE
    # timestamp is AFTER sim_start, so historical launches are never entered.
    async with aiohttp.ClientSession() as s:
        log.info("watch loop live — entering only launches after sim start")
        while True:
            now = time.time()
            if now - wl_ts > 600:   # refresh watchlist every 10 min
                wl = watchlist(a); deployers = [w for w,r in wl.items() if r=="deployer"]; funders=[w for w,r in wl.items() if r=="funder"]; wl_ts = now

            # ── DETECT: deployer launches (round-robin a few per sweep) ──────────
            for w in (deployers[di:di+5] or deployers[:5]):
                for t in await helius(s, w, "CREATE", 8):
                    ts = t.get("timestamp") or 0
                    if ts < sim_start: continue
                    m = _new_mint(t)
                    if m and m not in seen:
                        await enter(s, db, m, w, "deployer", ts)
                        seen.add(m); db.execute("INSERT OR IGNORE INTO seen_mints VALUES (?,?)", (m, now))
            di = (di + 5) % max(1, len(deployers))

            # ── DETECT: funder → fresh wallet → deploy ───────────────────────────
            for w in (funders[fi:fi+5] or funders[:5]):
                for t in await helius(s, w, None, 8):
                    ts = t.get("timestamp") or 0
                    if ts < sim_start: continue
                    for nt in t.get("nativeTransfers") or []:
                        if nt.get("fromUserAccount") == w and (nt.get("amount") or 0) > 5e7:  # >0.05 SOL
                            cand = nt.get("toUserAccount")
                            if cand:
                                db.execute("INSERT OR IGNORE INTO funder_candidates VALUES (?,?,?)", (cand, w, now))
            fi = (fi + 5) % max(1, len(funders))
            # check candidates for a CREATE within the window
            for cand, fund, since in db.execute("SELECT wallet, funder, since FROM funder_candidates WHERE ? - since < ?", (now, FUNDER_WINDOW)).fetchall():
                for t in await helius(s, cand, "CREATE", 4):
                    ts = t.get("timestamp") or 0
                    if ts < since: continue
                    m = _new_mint(t)
                    if m and m not in seen:
                        await enter(s, db, m, fund, "funder", ts)
                        seen.add(m); db.execute("INSERT OR IGNORE INTO seen_mints VALUES (?,?)", (m, now))
            db.execute("DELETE FROM funder_candidates WHERE ? - since > ?", (now, FUNDER_WINDOW))

            # ── TRACK open shadow trades ─────────────────────────────────────────
            for mint, op, role, ets, ep, peak in db.execute(
                    "SELECT mint, operator, role, entry_ts, entry_price, peak_price FROM shadow_trades WHERE status='open'").fetchall():
                p = await price_of(s, mint)
                if p <= 0:
                    if now - ets > HOLD_CAP_H * 3600:
                        db.execute("UPDATE shadow_trades SET status='no_price', exit_ts=? WHERE mint=?", (now, mint))
                    continue
                if ep <= 0:   # was pending — first real price becomes the entry
                    db.execute("UPDATE shadow_trades SET entry_price=?, peak_price=? WHERE mint=?", (p, p, mint))
                    continue
                newpeak = max(peak or ep, p)
                mult = newpeak / ep if ep > 0 else 0
                if mult >= TP_MULT:
                    db.execute("UPDATE shadow_trades SET status='WIN', peak_price=?, peak_mult=?, exit_ts=?, time_to_tp_s=? WHERE mint=?",
                               (newpeak, mult, now, now - ets, mint))
                    log.info("WIN  %s op=%s peak=%.2fx t=%ds", mint[:8], op[:6], mult, int(now-ets))
                elif p <= ep * RUG_MULT:
                    db.execute("UPDATE shadow_trades SET status='RUG', peak_price=?, peak_mult=?, exit_ts=? WHERE mint=?",
                               (newpeak, mult, now, mint))
                    log.info("RUG  %s op=%s peak=%.2fx", mint[:8], op[:6], mult)
                elif now - ets > HOLD_CAP_H * 3600:
                    db.execute("UPDATE shadow_trades SET status='TIMEOUT', peak_price=?, peak_mult=?, exit_ts=? WHERE mint=?",
                               (newpeak, mult, now, mint))
                else:
                    db.execute("UPDATE shadow_trades SET peak_price=?, peak_mult=? WHERE mint=?", (newpeak, mult, mint))
            db.commit()
            await asyncio.sleep(DETECT_EVERY)

async def enter(session, db, mint, operator, role, launch_ts):
    if db.execute("SELECT 1 FROM shadow_trades WHERE mint=?", (mint,)).fetchone():
        return
    price = await price_of(session, mint)
    now = time.time()
    if price <= 0:
        # can't price yet — record as pending; tracker will pick it up if it lists
        db.execute("INSERT OR IGNORE INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (mint, operator, role, now, 0, 0, 0, "open", None, None, now - launch_ts))
        log.info("ENTER(pending price) %s op=%s role=%s", mint[:8], operator[:6], role)
        db.commit(); return
    db.execute("INSERT OR IGNORE INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (mint, operator, role, now, price, price, 1.0, "open", None, None, now - launch_ts))
    log.info("ENTER %s op=%s role=%s entry=%.3e lag=%ds", mint[:8], operator[:6], role, price, int(now - launch_ts))
    db.commit()

if __name__ == "__main__":
    asyncio.run(main())

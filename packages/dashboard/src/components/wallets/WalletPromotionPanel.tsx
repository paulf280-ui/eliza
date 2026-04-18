import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'

type Verdict = 'PROMOTE' | 'HOLD' | 'BLACKLIST' | 'WATCH'

interface PromotionRow {
  wallet: string
  trades: number
  wr_pct: number
  net_sol: number
  median_pnl_pct: number
  last_ts: number
  verdict: Verdict
}

interface PromotionPayload {
  generated_ts: number
  total_wallets: number
  promote_count: number
  blacklist_count: number
  rows: PromotionRow[]
  watched: string[]
}

function verdictStyle(v: Verdict): { bg: string; color: string; label: string } {
  switch (v) {
    case 'PROMOTE':   return { bg: 'rgba(52,211,153,0.18)', color: '#34d399', label: '✓ PROMOTE' }
    case 'WATCH':     return { bg: 'rgba(249,115,22,0.18)', color: '#fb923c', label: '◐ WATCH' }
    case 'HOLD':      return { bg: 'rgba(148,163,184,0.15)', color: '#94a3b8', label: '· HOLD' }
    case 'BLACKLIST': return { bg: 'rgba(239,68,68,0.15)',  color: '#f87171', label: '✕ BLACKLIST' }
  }
}

function formatAge(ts: number): string {
  if (!ts) return '—'
  const mins = Math.floor((Date.now() / 1000 - ts) / 60)
  if (mins < 60)   return `${mins}m`
  if (mins < 1440) return `${Math.floor(mins / 60)}h`
  return `${Math.floor(mins / 1440)}d`
}

export default function WalletPromotionPanel() {
  const [payload, setPayload] = useState<PromotionPayload | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  const load = async () => {
    setRefreshing(true)
    try {
      const res = await fetch('/api/wallet-promotion')
      if (res.ok) setPayload(await res.json())
    } catch { /* ignore */ }
    finally { setRefreshing(false) }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 60_000) // refresh every 60s
    return () => clearInterval(id)
  }, [])

  const rows = payload?.rows ?? []
  const watched = new Set(payload?.watched ?? [])
  const promote = rows.filter(r => r.verdict === 'PROMOTE' && !watched.has(r.wallet))
  const watch   = rows.filter(r => r.verdict === 'WATCH'   && !watched.has(r.wallet))
  const display = expanded ? rows : [...promote, ...watch].slice(0, 8)

  return (
    <GlassCard className="overflow-hidden">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">
            Wallet Promotion Board
          </span>
          {promote.length > 0 && (
            <span className="bg-emerald-500/20 text-emerald-400 rounded-full px-2 py-0.5 text-[10px] font-bold">
              {promote.length} ready
            </span>
          )}
          {refreshing && <span className="text-[10px] text-zinc-500 animate-pulse">refreshing…</span>}
        </div>
        <div className="flex items-center gap-3 text-[10px] text-zinc-500">
          <span>watched: <span className="text-orange-400 font-mono">{watched.size}/4</span></span>
          <span>·</span>
          <span>total: <span className="text-zinc-300 font-mono">{payload?.total_wallets ?? 0}</span></span>
          <button
            onClick={() => setExpanded(e => !e)}
            className="ml-2 text-zinc-400 hover:text-zinc-200 transition-colors"
          >
            {expanded ? '▲ collapse' : '▼ show all'}
          </button>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="text-center py-6 text-zinc-600 text-sm">
          No paper-trade data yet — candidates appear after ≥3 trades per wallet.
        </div>
      ) : display.length === 0 ? (
        <div className="text-center py-4 text-zinc-600 text-xs">
          All PROMOTE candidates are already in WATCHED. No new promotions pending.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-zinc-500 border-b border-zinc-800">
                <th className="text-left pb-2 font-medium">Wallet</th>
                <th className="text-left pb-2 font-medium">Verdict</th>
                <th className="text-right pb-2 font-medium">Trades</th>
                <th className="text-right pb-2 font-medium">WR%</th>
                <th className="text-right pb-2 font-medium">Net SOL</th>
                <th className="text-right pb-2 font-medium">Med %</th>
                <th className="text-right pb-2 font-medium">Last</th>
              </tr>
            </thead>
            <tbody>
              {display.map(row => {
                const v = verdictStyle(row.verdict)
                const isWatched = watched.has(row.wallet)
                return (
                  <tr key={row.wallet}
                    className="border-b border-zinc-800/50 hover:bg-zinc-800/20 transition-colors">
                    <td className="py-2 font-mono text-zinc-200">
                      <div className="flex items-center gap-1.5">
                        <span>{row.wallet}</span>
                        {isWatched && (
                          <span className="text-[9px] px-1 py-0.5 rounded font-bold"
                            style={{ background: 'rgba(249,115,22,0.15)', color: '#fb923c' }}
                            title="Already in WATCHED_WALLETS">
                            ● watched
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-2">
                      <span className="text-[10px] px-1.5 py-0.5 rounded font-bold"
                        style={{ background: v.bg, color: v.color }}>
                        {v.label}
                      </span>
                    </td>
                    <td className="py-2 text-right font-mono text-zinc-300">{row.trades}</td>
                    <td className="py-2 text-right font-mono text-zinc-300">{row.wr_pct.toFixed(1)}</td>
                    <td className={`py-2 text-right font-mono font-semibold ${row.net_sol >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {row.net_sol >= 0 ? '+' : ''}{row.net_sol.toFixed(3)}
                    </td>
                    <td className={`py-2 text-right font-mono ${row.median_pnl_pct >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
                      {row.median_pnl_pct >= 0 ? '+' : ''}{row.median_pnl_pct.toFixed(0)}%
                    </td>
                    <td className="py-2 text-right font-mono text-zinc-500 text-[10px]">
                      {formatAge(row.last_ts)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {promote.length > 0 && (
        <div className="mt-3 pt-3 border-t border-zinc-800 text-[10px] text-zinc-500 flex items-center gap-2">
          <span className="text-emerald-400">●</span>
          <span>
            Ready for WATCHED slot {watched.size + 1}: consider
            {' '}
            <span className="text-emerald-400 font-semibold">
              {promote.slice(0, 2).map(p => p.wallet).join(', ')}
            </span>
            {promote.length > 2 && <span className="text-zinc-500"> +{promote.length - 2} more</span>}
            {' '}
            — manual review required (blacklist check, volume sanity).
          </span>
        </div>
      )}
    </GlassCard>
  )
}

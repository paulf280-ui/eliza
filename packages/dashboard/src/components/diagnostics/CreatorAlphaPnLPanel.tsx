import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'
import { fetchPerformanceBySource, type PerformanceBySourceResponse } from '../../api/client'

function CreatorAlphaPnLPanel() {
  const [data, setData] = useState<PerformanceBySourceResponse | null>(null)
  const [hours, setHours] = useState<number>(24)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const d = await fetchPerformanceBySource(hours)
        if (alive) { setData(d); setErr(null) }
      } catch (e) { if (alive) setErr(String(e)) }
    }
    load()
    const id = setInterval(load, 10000)
    return () => { alive = false; clearInterval(id) }
  }, [hours])

  if (err) return <GlassCard className="h-full"><div className="text-red-400 text-sm">Strategy P&L: {err}</div></GlassCard>
  if (!data) return <GlassCard className="h-full"><div className="text-zinc-500 text-sm">loading strategy P&L…</div></GlassCard>

  // All active strategies: graduation snipe (lifecycle) + creator-alpha BC
  const allSources = data.sources.filter(s =>
    s.source.startsWith('creator_alpha') ||
    s.source.startsWith('lifecycle') ||
    s.source.startsWith('graduation')
  )

  const trades   = allSources.reduce((a, s) => a + s.trades, 0)
  const wins     = allSources.reduce((a, s) => a + s.wins, 0)
  const losses   = allSources.reduce((a, s) => a + s.losses, 0)
  const flat     = allSources.reduce((a, s) => a + s.flat, 0)
  const totalPnl = allSources.reduce((a, s) => a + s.total_pnl_sol, 0)
  const wr       = trades > 0 ? (wins / trades * 100) : 0
  const avgPnl   = trades > 0
    ? (allSources.reduce((a, s) => a + s.avg_pnl_pct * s.trades, 0) / trades)
    : 0
  const best  = allSources.length ? Math.max(...allSources.map(s => s.best_pct))  : 0
  const worst = allSources.length ? Math.min(...allSources.map(s => s.worst_pct)) : 0

  // Recent trades — flatten and sort newest first
  const allRecent = allSources
    .flatMap(s => (s.recent_trades || []).map(t => ({ ...t, source: s.source })))
    .sort((a, b) => (b.ts_close || 0) - (a.ts_close || 0))
    .slice(0, 12)

  // Strategy label helper
  const sourceLabel = (src: string) => {
    if (src.startsWith('lifecycle') || src.startsWith('graduation')) return 'grad-snipe'
    if (src.includes('operator')) return 'operator'
    if (src.includes('direct'))   return 'direct'
    return src.replace('creator_alpha_', '')
  }

  const wrColor = wr >= 50 ? '#6ee7b7' : wr >= 30 ? '#fb923c' : '#f87171'

  return (
    <GlassCard className="h-full overflow-hidden flex flex-col">
      {/* Header */}
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-widest font-semibold" style={{ color: '#34d399' }}>
            📈 STRATEGY P&amp;L — Fresh Start
          </div>
          <div className="text-zinc-500 text-xs mt-0.5">
            Graduation snipe (PumpSwap) + creator-alpha · +60% TP · $15K+ liq · data-proven
          </div>
        </div>
        <div className="flex items-center gap-2">
          {[24, 72, 168].map(h => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className="px-2 py-0.5 rounded text-[11px]"
              style={{
                background: hours === h ? 'rgba(52,211,153,0.18)' : 'rgba(82,82,91,0.2)',
                border: `1px solid ${hours === h ? 'rgba(52,211,153,0.4)' : 'rgba(82,82,91,0.4)'}`,
                color: hours === h ? '#6ee7b7' : '#a1a1aa',
              }}
            >
              {h === 24 ? '24h' : h === 72 ? '3d' : '7d'}
            </button>
          ))}
        </div>
      </div>

      {trades === 0 ? (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center space-y-2">
            <div className="text-zinc-400 text-sm font-semibold">Fresh start — clean slate</div>
            <div className="text-zinc-600 text-xs max-w-[280px]">
              Watching 45 operators on PumpSwap graduation events.<br/>
              Entry: $25K–$300K MC · liq ≥$15K · LP burned · 21/21 win rate in data.
            </div>
            <div className="text-[10px] text-zinc-700 mt-2">
              Previous 36 trades archived · history reset 2026-05-06
            </div>
          </div>
        </div>
      ) : (
        <>
          {/* Hero stats */}
          <div className="grid grid-cols-4 gap-3 mb-3">
            <div className="bg-zinc-800/40 rounded-md p-3">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">trades</div>
              <div className="text-zinc-100 font-mono text-2xl mt-0.5">{trades}</div>
              <div className="text-[10px] text-zinc-500 mt-0.5">
                <span className="text-emerald-400">{wins}W</span>
                {' · '}
                <span className="text-red-400">{losses}L</span>
                {flat > 0 && <span className="text-zinc-500"> · {flat}~</span>}
              </div>
            </div>

            <div className="bg-zinc-800/40 rounded-md p-3">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">win rate</div>
              <div className="font-mono text-2xl mt-0.5" style={{ color: wrColor }}>
                {wr.toFixed(1)}%
              </div>
              <div className="text-[10px] text-zinc-500 mt-0.5">target ≥50%</div>
            </div>

            <div className="bg-zinc-800/40 rounded-md p-3">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">total P&amp;L</div>
              <div className="font-mono text-2xl mt-0.5" style={{ color: totalPnl >= 0 ? '#6ee7b7' : '#f87171' }}>
                {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(4)}
              </div>
              <div className="text-[10px] text-zinc-500 mt-0.5">SOL · avg {avgPnl > 0 ? '+' : ''}{avgPnl.toFixed(1)}%</div>
            </div>

            <div className="bg-zinc-800/40 rounded-md p-3">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">best / worst</div>
              <div className="font-mono mt-0.5">
                <span className="text-emerald-400 text-base">+{best.toFixed(0)}%</span>
                <span className="text-zinc-600 mx-1">/</span>
                <span className="text-red-400 text-base">{worst.toFixed(0)}%</span>
              </div>
              <div className="text-[10px] text-zinc-500 mt-0.5">single-trade</div>
            </div>
          </div>

          {/* Per-source breakdown */}
          {allSources.length > 1 && (
            <div className="grid grid-cols-2 gap-2 mb-3">
              {allSources.map(s => (
                <div key={s.source} className="bg-zinc-800/30 rounded px-2 py-1.5 text-[11px]">
                  <div className="font-mono text-zinc-300">{sourceLabel(s.source)}</div>
                  <div className="flex justify-between text-[10px] text-zinc-500 mt-0.5">
                    <span>{s.trades}T · {s.win_rate_pct}% WR</span>
                    <span style={{ color: s.total_pnl_sol >= 0 ? '#6ee7b7' : '#f87171' }}>
                      {s.total_pnl_sol >= 0 ? '+' : ''}{s.total_pnl_sol.toFixed(4)} SOL
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Recent trades */}
          <div className="flex-1 overflow-y-auto">
            <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1.5">recent trades</div>
            <div className="space-y-0.5">
              {allRecent.map((t, i) => {
                const profitable = t.pnl_pct >= 0
                const isGrad = t.source.startsWith('lifecycle') || t.source.startsWith('graduation')
                return (
                  <div key={i} className="grid grid-cols-[1fr_60px_70px_70px_80px] gap-2 text-[11px] py-0.5 border-b border-zinc-800/40">
                    <span className="font-mono text-zinc-300 truncate">{t.token || t.source.slice(-8)}</span>
                    <span className="text-[10px]" style={{ color: isGrad ? '#60a5fa' : '#a78bfa' }}>
                      {isGrad ? '🎓grad' : '⚡alpha'}
                    </span>
                    <span className="font-mono" style={{ color: profitable ? '#6ee7b7' : '#f87171' }}>
                      {profitable ? '+' : ''}{t.pnl_pct.toFixed(1)}%
                    </span>
                    <span className="font-mono text-zinc-500 text-[10px]">pk +{t.peak_pct.toFixed(0)}%</span>
                    <span className="text-zinc-600 text-[10px]">
                      {t.ts_close ? new Date(t.ts_close * 1000).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' }) : '—'}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}
    </GlassCard>
  )
}

export default CreatorAlphaPnLPanel

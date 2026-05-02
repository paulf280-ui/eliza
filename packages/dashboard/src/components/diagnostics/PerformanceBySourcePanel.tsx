import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'
import { fetchPerformanceBySource, type PerformanceBySourceResponse } from '../../api/client'

const SOURCE_COLORS: Record<string, string> = {
  lifecycle:                 'rgb(96, 165, 250)',   // blue
  lifecycle_bounce:          'rgb(167, 139, 250)',  // purple
  lifecycle_viral:           'rgb(251, 191, 36)',   // amber
  breakout_candle:           'rgb(248, 113, 113)',  // red
  cluster_confirm:           'rgb(244, 114, 182)',  // pink
  creator_alpha_direct:      'rgb(52, 211, 153)',   // emerald
  creator_alpha_operator:    'rgb(45, 212, 191)',   // teal
}

export default function PerformanceBySourcePanel() {
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
    const id = setInterval(load, 30000)
    return () => { alive = false; clearInterval(id) }
  }, [hours])

  if (err) return <GlassCard className="h-full"><div className="text-red-400 text-sm">performance: {err}</div></GlassCard>
  if (!data) return <GlassCard className="h-full"><div className="text-zinc-500 text-sm">loading performance…</div></GlassCard>

  const totals = data.totals
  const sources = data.sources

  return (
    <GlassCard className="h-full overflow-hidden flex flex-col">
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-400 font-semibold">
            Performance by signal source
          </div>
          <div className="text-zinc-500 text-xs mt-0.5">
            which strategy path actually makes money
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs">
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

      {/* Totals row */}
      <div className="grid grid-cols-4 gap-3 mb-3 text-xs">
        <div className="bg-zinc-800/30 rounded px-2 py-1.5">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">trades</div>
          <div className="text-zinc-100 font-mono text-base">{totals.trades}</div>
        </div>
        <div className="bg-zinc-800/30 rounded px-2 py-1.5">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">win rate</div>
          <div className="font-mono text-base" style={{ color: totals.overall_wr_pct >= 50 ? '#6ee7b7' : '#fb923c' }}>
            {totals.overall_wr_pct.toFixed(1)}%
          </div>
        </div>
        <div className="bg-zinc-800/30 rounded px-2 py-1.5">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">total P&amp;L</div>
          <div className="font-mono text-base" style={{ color: totals.total_pnl_sol >= 0 ? '#6ee7b7' : '#f87171' }}>
            {totals.total_pnl_sol >= 0 ? '+' : ''}{totals.total_pnl_sol.toFixed(4)} SOL
          </div>
        </div>
        <div className="bg-zinc-800/30 rounded px-2 py-1.5">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">window</div>
          <div className="text-zinc-100 font-mono text-base">{totals.window_hours.toFixed(0)}h</div>
        </div>
      </div>

      {/* Per-source breakdown */}
      <div className="flex-1 overflow-y-auto">
        {sources.length === 0 ? (
          <div className="text-zinc-600 text-sm italic py-4 text-center">
            no closed trades in this window
          </div>
        ) : (
          <div className="space-y-2">
            {sources.map(s => {
              const color = SOURCE_COLORS[s.source] || 'rgb(161, 161, 170)'
              const profitable = s.total_pnl_sol >= 0
              return (
                <div key={s.source} className="bg-zinc-800/40 rounded px-3 py-2">
                  <div className="flex items-baseline justify-between mb-1.5">
                    <div className="flex items-center gap-2">
                      <span className="w-2 h-2 rounded-full" style={{ background: color }} />
                      <span className="font-mono text-sm text-zinc-200">{s.source}</span>
                      <span className="text-[10px] text-zinc-500">{s.trades} trades</span>
                    </div>
                    <div className="font-mono text-sm" style={{ color: profitable ? '#6ee7b7' : '#f87171' }}>
                      {profitable ? '+' : ''}{s.total_pnl_sol.toFixed(4)} SOL
                    </div>
                  </div>
                  <div className="grid grid-cols-5 gap-2 text-[11px]">
                    <div>
                      <div className="text-zinc-500 text-[10px]">WR</div>
                      <div className="font-mono" style={{ color: s.win_rate_pct >= 50 ? '#6ee7b7' : '#fb923c' }}>
                        {s.win_rate_pct}%
                      </div>
                    </div>
                    <div>
                      <div className="text-zinc-500 text-[10px]">avg PnL</div>
                      <div className="font-mono text-zinc-300">{s.avg_pnl_pct > 0 ? '+' : ''}{s.avg_pnl_pct}%</div>
                    </div>
                    <div>
                      <div className="text-zinc-500 text-[10px]">avg peak</div>
                      <div className="font-mono text-zinc-300">+{s.avg_peak_pct}%</div>
                    </div>
                    <div>
                      <div className="text-zinc-500 text-[10px]">best</div>
                      <div className="font-mono text-emerald-400">+{s.best_pct}%</div>
                    </div>
                    <div>
                      <div className="text-zinc-500 text-[10px]">worst</div>
                      <div className="font-mono text-red-400">{s.worst_pct}%</div>
                    </div>
                  </div>
                  <div className="mt-1 text-[10px] text-zinc-500">
                    W <span className="text-emerald-400 font-mono">{s.wins}</span> ·
                    L <span className="text-red-400 font-mono ml-0.5">{s.losses}</span> ·
                    flat <span className="text-zinc-400 font-mono ml-0.5">{s.flat}</span>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </GlassCard>
  )
}

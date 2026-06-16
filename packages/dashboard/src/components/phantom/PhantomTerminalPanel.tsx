import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'

// Repurposed: live window into the Phase-1 shadow copy-trade sim (paper, TP +75%).
interface ShadowTrade {
  mint: string; operator: string; role: string; status: string
  peak_mult: number; entry_ts: number; time_to_tp: number | null; lag: number | null
}
interface ShadowData {
  running: boolean; elapsed_h?: number; tp_pct?: number
  summary?: { total: number; open: number; wins: number; rugs: number; timeout: number; no_price: number }
  closed?: number; hit_rate?: number | null; avg_time_to_tp_min?: number | null; avg_lag_s?: number | null
  trades?: ShadowTrade[]; per_operator?: { operator: string; role: string; total: number; wins: number }[]
}

const PHANTOM = 'https://trade.phantom.com'
const openChart = (mint: string) =>
  window.open(`${PHANTOM}/sol/${mint}`, 'phantom_terminal', 'width=1400,height=900,left=80,top=60')

const ST: Record<string, { c: string; l: string }> = {
  open: { c: '#a78bfa', l: 'LIVE' }, WIN: { c: '#10b981', l: 'WIN' },
  RUG: { c: '#ef4444', l: 'RUG' }, TIMEOUT: { c: '#f59e0b', l: 'FLAT' },
  no_price: { c: '#52525b', l: 'n/a' },
}
const short = (w: string) => w.slice(0, 4) + '..' + w.slice(-3)
const ago = (ts: number) => {
  const s = Math.max(0, Date.now() / 1000 - ts)
  return s < 60 ? `${s | 0}s` : s < 3600 ? `${(s / 60) | 0}m` : `${(s / 3600) | 0}h`
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex-1 rounded-lg px-3 py-2" style={{ background: 'rgba(24,24,27,0.5)', border: '1px solid rgba(82,82,91,0.3)' }}>
      <div className="text-[9px] uppercase tracking-widest text-zinc-600">{label}</div>
      <div className="text-lg font-bold" style={{ color: color || '#e4e4e7' }}>{value}</div>
    </div>
  )
}

export default function PhantomTerminalPanel() {
  const [d, setD] = useState<ShadowData>({ running: false })
  const [, tick] = useState(0)
  useEffect(() => {
    let on = true
    const load = async () => { try { const r = await fetch('/api/shadow'); const j = await r.json(); if (on) setD(j) } catch { /**/ } }
    load(); const id = setInterval(load, 4000)
    const t = setInterval(() => tick(x => x + 1), 1000) // age ticker
    return () => { on = false; clearInterval(id); clearInterval(t) }
  }, [])

  const s = d.summary
  return (
    <GlassCard className="h-full flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-center justify-between flex-shrink-0">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-400 font-semibold">
            Shadow Sim <span className="text-purple-400">· paper copy-trade</span>
          </div>
          <div className="text-zinc-500 text-xs mt-0.5">
            following live operators · TP +{d.tp_pct ?? 75}% · {d.running ? `${d.elapsed_h ?? 0}h elapsed` : 'offline'}
          </div>
        </div>
        <div className="flex items-center gap-1.5 text-[10px] text-zinc-500">
          <span className="w-2 h-2 rounded-full" style={{ background: d.running ? '#10b981' : '#52525b' }} />
          {d.running ? 'live' : 'off'}
        </div>
      </div>

      {/* Stat row */}
      {d.running && (
        <div className="flex gap-2 flex-shrink-0">
          <Stat label="Trades" value={String(s?.total ?? 0)} />
          <Stat label="+75% Hit" value={d.hit_rate != null ? `${d.hit_rate}%` : '—'} color={d.hit_rate != null && d.hit_rate >= 50 ? '#10b981' : d.hit_rate != null ? '#f59e0b' : undefined} />
          <Stat label="Live" value={String(s?.open ?? 0)} color="#a78bfa" />
          <Stat label="Wins" value={String(s?.wins ?? 0)} color="#10b981" />
          <Stat label="Rugs" value={String(s?.rugs ?? 0)} color="#ef4444" />
        </div>
      )}
      {d.running && (d.avg_lag_s != null || d.avg_time_to_tp_min != null) && (
        <div className="flex-shrink-0 text-[10px] text-zinc-600 flex gap-4 px-1">
          {d.avg_lag_s != null && <span>avg detect lag: <span className="text-zinc-400">{d.avg_lag_s}s</span></span>}
          {d.avg_time_to_tp_min != null && <span>avg time→+75%: <span className="text-zinc-400">{d.avg_time_to_tp_min}m</span></span>}
        </div>
      )}

      {/* Live trades */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {!d.running ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 py-8 text-center">
            <div className="text-zinc-700 text-3xl">👻</div>
            <div className="text-zinc-600 text-sm">Shadow sim is offline.</div>
          </div>
        ) : !d.trades?.length ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 py-8 text-center">
            <div className="text-zinc-700 text-3xl">🎯</div>
            <div className="text-zinc-600 text-sm">
              Watching operators — no launches caught yet.<br />
              <span className="text-zinc-700 text-xs">A row appears the instant a tracked operator deploys.</span>
            </div>
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[9px] uppercase tracking-wider text-zinc-600 text-left">
                <th className="pb-1 font-medium">Token</th>
                <th className="pb-1 font-medium">Operator</th>
                <th className="pb-1 font-medium text-right">Peak</th>
                <th className="pb-1 font-medium text-right">Age</th>
                <th className="pb-1 font-medium text-right">Status</th>
              </tr>
            </thead>
            <tbody>
              {d.trades.map(t => {
                const st = ST[t.status] || ST.open
                const peak = t.peak_mult ? `${t.peak_mult.toFixed(2)}×` : '—'
                return (
                  <tr key={t.mint} className="border-t border-zinc-800/40 hover:bg-purple-500/5">
                    <td className="py-1.5">
                      <button onClick={() => openChart(t.mint)} className="font-mono text-zinc-300 hover:text-purple-300">
                        {t.mint.slice(0, 6)}… ↗
                      </button>
                    </td>
                    <td className="py-1.5 font-mono text-zinc-500">
                      {short(t.operator)}<span className="text-zinc-700"> · {t.role === 'funder' ? 'F' : 'D'}</span>
                    </td>
                    <td className="py-1.5 text-right font-mono" style={{ color: (t.peak_mult ?? 0) >= 1.75 ? '#10b981' : '#a1a1aa' }}>{peak}</td>
                    <td className="py-1.5 text-right text-zinc-600">{ago(t.entry_ts)}</td>
                    <td className="py-1.5 text-right">
                      <span className="px-1.5 py-0.5 rounded text-[9px] font-bold"
                        style={{ background: st.c + '22', color: st.c, border: `1px solid ${st.c}55` }}>{st.l}</span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Per-operator hit rates */}
      {d.per_operator && d.per_operator.length > 0 && (
        <div className="flex-shrink-0 border-t border-zinc-800/50 pt-2">
          <div className="text-[9px] uppercase tracking-widest text-zinc-600 mb-1">Operator hit rate (resolved)</div>
          <div className="flex flex-wrap gap-1.5">
            {d.per_operator.map(o => {
              const wr = o.total ? Math.round((100 * o.wins) / o.total) : 0
              return (
                <span key={o.operator} className="text-[10px] font-mono px-2 py-0.5 rounded"
                  style={{ background: 'rgba(24,24,27,0.6)', color: wr >= 50 ? '#10b981' : '#a1a1aa', border: '1px solid rgba(82,82,91,0.3)' }}>
                  {short(o.operator)} {o.wins}/{o.total}
                </span>
              )
            })}
          </div>
        </div>
      )}
    </GlassCard>
  )
}

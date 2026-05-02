import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'
import { fetchCreatorAlpha, type CreatorAlphaResponse } from '../../api/client'

const KIND_META: Record<string, { icon: string; color: string; label: string }> = {
  direct_create:    { icon: '🎯', color: '#fb923c', label: 'DIRECT CREATE' },
  operator_fund:    { icon: '👁',  color: '#60a5fa', label: 'OPERATOR FUND' },
  operator_create:  { icon: '🎯', color: '#a78bfa', label: 'OPERATOR CREATE' },
  graduated:        { icon: '🚀', color: '#34d399', label: 'GRADUATED' },
}

function fmtAge(secs: number): string {
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m`
  return `${Math.floor(secs / 3600)}h${Math.floor((secs % 3600) / 60)}m`
}

function fmtTime(ts: number | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function CreatorAlphaPanel() {
  const [data, setData] = useState<CreatorAlphaResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const d = await fetchCreatorAlpha()
        if (alive) { setData(d); setErr(null) }
      } catch (e) { if (alive) setErr(String(e)) }
    }
    load()
    const id = setInterval(load, 5000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  if (err) return <GlassCard className="h-full"><div className="text-red-400 text-sm">creator-alpha: {err}</div></GlassCard>
  if (!data) return <GlassCard className="h-full"><div className="text-zinc-500 text-sm">loading creator-alpha…</div></GlassCard>

  const recent = [...(data.recent || [])].reverse()  // newest first

  return (
    <GlassCard className="h-full overflow-hidden flex flex-col">
      {/* Header */}
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-400 font-semibold">
            Creator-Alpha Live
          </div>
          <div className="text-zinc-500 text-xs mt-0.5">
            operator + direct-creator wallet monitor · refresh 5s
          </div>
        </div>
        <div className="text-right text-xs text-zinc-400">
          <span className="text-orange-400">{data.tracked_direct}</span> direct ·
          <span className="text-blue-400 ml-1">{data.tracked_operators}</span> operators ·
          <span className="text-purple-400 ml-1">{data.watched_count}</span> watching ·
          <span className="text-emerald-400 ml-1">{data.pending_count}</span> pending
        </div>
      </div>

      {/* 3-column body: operators / pending mints / activity feed */}
      <div className="grid grid-cols-3 gap-3 flex-1 overflow-hidden">
        {/* Operators */}
        <div className="overflow-y-auto pr-2">
          <div className="text-[11px] uppercase tracking-wider text-zinc-500 font-semibold mb-1.5">
            Operators ({data.operators.length})
          </div>
          <div className="space-y-1.5">
            {data.operators.length === 0 ? (
              <div className="text-xs text-zinc-600 italic">no activity yet</div>
            ) : data.operators
                .sort((a, b) => b.last_fund_ts - a.last_fund_ts)
                .map(op => (
              <div key={op.wallet} className="bg-zinc-800/40 rounded px-2 py-1.5 text-xs">
                <div className="font-mono text-zinc-200 truncate">{op.wallet.slice(0, 12)}…</div>
                <div className="text-[10px] text-zinc-500 mt-0.5 flex justify-between">
                  <span>last: {fmtTime(op.last_fund_ts)}</span>
                  <span>
                    <span className="text-blue-400">{op.fund_count}</span>f ·
                    <span className="text-purple-400 ml-1">{op.create_count}</span>c ·
                    <span className="text-emerald-400 ml-1">{op.graduated_count}</span>g
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Pending mints */}
        <div className="overflow-y-auto pr-2">
          <div className="text-[11px] uppercase tracking-wider text-zinc-500 font-semibold mb-1.5">
            Pending mints ({data.pending_mints.length})
          </div>
          <div className="space-y-1.5">
            {data.pending_mints.length === 0 ? (
              <div className="text-xs text-zinc-600 italic">no pending mints</div>
            ) : data.pending_mints
                .sort((a, b) => b.detected_ts - a.detected_ts)
                .map(m => {
              const ttlPct = (m.ttl_remaining_secs / (60 * 60)) * 100
              return (
                <div key={m.mint} className="bg-zinc-800/40 rounded px-2 py-1.5 text-xs">
                  <div className="font-mono text-zinc-200 text-[11px] truncate">{m.mint}</div>
                  <div className="text-[10px] text-zinc-500 mt-0.5 flex justify-between">
                    <span>{m.source.replace('creator_alpha_', '')} · age {fmtAge(m.age_secs)}</span>
                    <span className={ttlPct < 30 ? 'text-amber-400' : 'text-zinc-500'}>
                      ttl {fmtAge(m.ttl_remaining_secs)}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Activity feed */}
        <div className="overflow-y-auto pr-2">
          <div className="text-[11px] uppercase tracking-wider text-zinc-500 font-semibold mb-1.5">
            Activity feed
          </div>
          <div className="space-y-1">
            {recent.length === 0 ? (
              <div className="text-xs text-zinc-600 italic">no signals yet</div>
            ) : recent.map((s, i) => {
              const meta = KIND_META[s.kind] || { icon: '·', color: '#a1a1aa', label: s.kind }
              const ts = s.ts || s.detected_ts
              const target = s.mint || s.child || s.creator || ''
              return (
                <div key={i} className="text-[11px] flex items-baseline gap-1.5 leading-tight">
                  <span className="text-zinc-600 font-mono text-[10px] flex-shrink-0">{fmtTime(ts)}</span>
                  <span style={{ color: meta.color }}>{meta.icon}</span>
                  <span className="font-mono text-zinc-300 truncate">
                    {target.slice(0, 12)}…
                    {s.amount_sol != null && (
                      <span className="text-zinc-500 ml-1">{s.amount_sol.toFixed(2)} SOL</span>
                    )}
                    {s.lag_secs != null && (
                      <span className="text-emerald-400 ml-1">lag {s.lag_secs}s</span>
                    )}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </GlassCard>
  )
}

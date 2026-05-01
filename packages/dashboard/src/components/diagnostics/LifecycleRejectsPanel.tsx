import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'
import { fetchLifecycleRejects, type LifecycleRejectsResponse } from '../../api/client'

const REJECT_LABELS: Record<string, { label: string; tier: 'baseline' | 'momentum' | 'quality' | 'safety' }> = {
  age:        { label: 'Age window',        tier: 'baseline' },
  liq:        { label: 'Liquidity range',   tier: 'baseline' },
  mc:         { label: 'Market cap range',  tier: 'baseline' },
  lmratio:    { label: 'Liq/MC ratio',      tier: 'baseline' },
  h1_high:    { label: 'h1 too high',       tier: 'momentum' },
  h1_low:     { label: 'h1 too low',        tier: 'momentum' },
  wash:       { label: 'Wash-trading cap',  tier: 'momentum' },
  txn_thin:   { label: 'h1 txns < 30',      tier: 'quality'  },
  buy_ratio:  { label: 'Buy ratio range',   tier: 'quality'  },
  no_socials: { label: 'No socials',        tier: 'quality'  },
  holders:    { label: 'Holder count',      tier: 'quality'  },
  velocity:   { label: 'Buy velocity',      tier: 'quality'  },
  top1:       { label: 'top1 holder %',     tier: 'safety'   },
  top10:      { label: 'top10 holder %',    tier: 'safety'   },
}

const TIER_COLOR: Record<string, string> = {
  baseline: 'rgb(96, 165, 250)',   // blue
  momentum: 'rgb(251, 191, 36)',   // amber
  quality:  'rgb(167, 139, 250)',  // purple
  safety:   'rgb(248, 113, 113)',  // red
}

export default function LifecycleRejectsPanel() {
  const [data, setData] = useState<LifecycleRejectsResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const r = await fetchLifecycleRejects()
        if (alive) { setData(r); setErr(null) }
      } catch (e) {
        if (alive) setErr(String(e))
      }
    }
    load()
    const id = setInterval(load, 15000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  if (err) {
    return (
      <GlassCard className="h-full">
        <div className="text-zinc-400 text-sm">scout diagnostics — error: {err}</div>
      </GlassCard>
    )
  }
  if (!data) {
    return (
      <GlassCard className="h-full">
        <div className="text-zinc-500 text-sm">loading scout diagnostics…</div>
      </GlassCard>
    )
  }

  const totalRejects = Object.values(data.totals).reduce((a, b) => a + b, 0)
  const sortedKeys = Object.keys(data.totals).sort(
    (a, b) => (data.totals[b] || 0) - (data.totals[a] || 0)
  )
  const maxVal = Math.max(1, ...Object.values(data.totals))
  const passRate = data.candidates_total > 0
    ? (100 * (data.candidates_total - totalRejects) / data.candidates_total)
    : 0

  return (
    <GlassCard className="h-full">
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-400 font-semibold">
            Scout reject reasons — 24h
          </div>
          <div className="text-zinc-500 text-xs mt-0.5">
            why monster-lifecycle skipped tokens · refresh every 15s
          </div>
        </div>
        <div className="text-right text-xs text-zinc-400">
          {data.cycles} cycles · {data.candidates_total.toLocaleString()} candidates · {data.entered_total} entered
          {data.viral_fires_total != null && data.viral_fires_total > 0 && (
            <span className="ml-2 text-orange-400 font-semibold">· 🚀 {data.viral_fires_total} viral</span>
          )}
        </div>
      </div>

      {totalRejects === 0 ? (
        <div className="text-zinc-500 text-sm py-8 text-center">
          no rejects in window — bot just started or feed is silent
        </div>
      ) : (
        <div className="space-y-1.5">
          {sortedKeys.map(k => {
            const meta = REJECT_LABELS[k] || { label: k, tier: 'baseline' as const }
            const v = data.totals[k] || 0
            const pct = (v / maxVal) * 100
            const sharePct = totalRejects > 0 ? (v / totalRejects) * 100 : 0
            const color = TIER_COLOR[meta.tier]
            return (
              <div key={k} className="grid grid-cols-[140px_1fr_64px_56px] items-center gap-2">
                <div className="text-xs text-zinc-300 font-mono">{meta.label}</div>
                <div className="h-5 bg-zinc-800/50 rounded overflow-hidden relative">
                  <div
                    className="h-full transition-all duration-500"
                    style={{ width: `${pct}%`, background: color, opacity: 0.85 }}
                  />
                </div>
                <div className="text-xs text-zinc-200 font-mono text-right">{v.toLocaleString()}</div>
                <div className="text-[10px] text-zinc-500 text-right">{sharePct.toFixed(0)}%</div>
              </div>
            )
          })}
        </div>
      )}

      <div className="mt-4 pt-3 border-t border-zinc-800/60 grid grid-cols-3 gap-3 text-xs">
        <div>
          <div className="text-zinc-500 text-[10px] uppercase tracking-wider">total rejects</div>
          <div className="text-zinc-200 font-mono">{totalRejects.toLocaleString()}</div>
        </div>
        <div>
          <div className="text-zinc-500 text-[10px] uppercase tracking-wider">pass-through rate</div>
          <div className="text-zinc-200 font-mono">{passRate.toFixed(2)}%</div>
        </div>
        <div>
          <div className="text-zinc-500 text-[10px] uppercase tracking-wider">avg rej / cycle</div>
          <div className="text-zinc-200 font-mono">
            {data.cycles > 0 ? (totalRejects / data.cycles).toFixed(1) : '0'}
          </div>
        </div>
      </div>
    </GlassCard>
  )
}

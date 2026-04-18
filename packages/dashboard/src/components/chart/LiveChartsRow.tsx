import { useState, useEffect } from 'react'
import { useDashboardStore } from '../../store/dashboard'
import { GlassCard } from '../common/GlassCard'

const DEX_LABELS: Record<string, string> = {
  pumpswap: 'PumpSwap',
  raydium: 'Raydium',
  social_momentum: 'Social Snipe',
  pump_fun: 'Pump.fun',
}

const DEX_COLORS: Record<string, string> = {
  pumpswap: '#10b981',
  raydium:  '#818cf8',
  social_momentum: '#f59e0b',
  pump_fun: '#22d3ee',
}

interface ChartSlot {
  mint: string
  dex: string
  pnl_pct: number
  dismissed: boolean
}

export default function LiveChartsRow() {
  const { positions } = useDashboardStore()
  const [slots, setSlots] = useState<ChartSlot[]>([])

  // When positions change: add new ones, update P&L on existing, auto-remove closed ones
  useEffect(() => {
    const openMints = new Set(Object.keys(positions))
    setSlots(prev => {
      const prevMints = prev.map(s => s.mint)
      // New positions not yet in slots
      const newSlots: ChartSlot[] = Array.from(openMints)
        .filter(m => !prevMints.includes(m))
        .map(m => ({
          mint: m,
          dex: positions[m].dex,
          pnl_pct: positions[m].pnl_pct,
          dismissed: false,
        }))
      // Keep only slots whose position is still open — closed positions auto-removed
      const updated = prev
        .filter(s => openMints.has(s.mint))
        .map(s => ({
          ...s,
          pnl_pct: positions[s.mint]?.pnl_pct ?? s.pnl_pct,
          dex: positions[s.mint]?.dex ?? s.dex,
        }))
      return [...updated, ...newSlots]
    })
  }, [positions])

  const dismiss = (mint: string) =>
    setSlots(prev => prev.filter(s => s.mint !== mint))

  const visible = slots.filter(s => !s.dismissed).slice(0, 3)

  if (slots.length === 0) return null

  return (
    <div className="col-span-12 flex flex-col gap-3">
      {/* Section header */}
      <div className="flex items-center justify-between px-1">
        <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">
          Live Charts
          {visible.length > 0 && (
            <span className="ml-2 text-zinc-600 normal-case tracking-normal font-normal">
              — open positions only · auto-closes when trade exits
            </span>
          )}
        </span>
      </div>

      {/* Chart grid — 1 col mobile, 3 col desktop */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {visible.map(slot => {
          const accentColor = DEX_COLORS[slot.dex] ?? '#64748b'
          const pnlPositive = slot.pnl_pct >= 0
          const embedUrl = `https://dexscreener.com/solana/${slot.mint}?embed=1&theme=dark&trades=0&info=0`

          return (
            <GlassCard
              key={slot.mint}
              className="flex flex-col"
              style={{ borderColor: `${accentColor}30` }}
            >
              {/* Chart header */}
              <div className="flex items-center justify-between mb-2 flex-shrink-0">
                <div className="flex items-center gap-2 min-w-0">
                  <div className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: accentColor }} />
                  <span className="font-mono text-xs text-zinc-300 truncate">
                    {slot.mint.slice(0, 6)}…{slot.mint.slice(-4)}
                  </span>
                  <span className="text-[10px] text-zinc-600 flex-shrink-0">
                    {DEX_LABELS[slot.dex] ?? slot.dex}
                  </span>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <span className={`text-xs font-semibold tabular-nums ${pnlPositive ? 'text-emerald-400' : 'text-red-400'}`}>
                    {pnlPositive ? '+' : ''}{slot.pnl_pct.toFixed(1)}%
                  </span>
                  <a
                    href={`https://solscan.io/token/${slot.mint}`}
                    target="_blank"
                    rel="noreferrer"
                    className="text-zinc-600 hover:text-zinc-400 transition-colors text-xs"
                    title="Solscan"
                  >
                    ↗
                  </a>
                  {/* Dismiss button — closes this panel only */}
                  <button
                    onClick={() => dismiss(slot.mint)}
                    className="w-5 h-5 flex items-center justify-center rounded text-zinc-600 hover:text-zinc-300 hover:bg-zinc-700/50 transition-all text-xs"
                    title="Close chart"
                  >
                    ✕
                  </button>
                </div>
              </div>

              {/* DexScreener iframe */}
              <iframe
                key={slot.mint}
                src={embedUrl}
                className="w-full rounded-lg border-0"
                style={{ height: '340px' }}
                allow="clipboard-write"
                title={`Chart ${slot.mint}`}
              />
            </GlassCard>
          )
        })}
      </div>
    </div>
  )
}

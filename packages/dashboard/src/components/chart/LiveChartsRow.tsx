import { useState, useEffect } from 'react'
import { useDashboardStore } from '../../store/dashboard'
import { GlassCard } from '../common/GlassCard'
import { useLivePrice } from '../../hooks/useLivePrice'

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
  entry_price_sol: number
  entry_sol_spent: number
  dismissed: boolean
}

function LiveChartCard({ slot, onDismiss }: { slot: ChartSlot; onDismiss: () => void }) {
  const live = useLivePrice(slot.mint, slot.entry_price_sol, slot.entry_sol_spent, 3000)
  const pnlPct = live.pnlPct ?? slot.pnl_pct
  const pnlSol = live.pnlSol
  const pnlPositive = pnlPct >= 0
  const accentColor = DEX_COLORS[slot.dex] ?? '#64748b'
  const isStale = live.ageSecs > 8

  return (
    <GlassCard className="flex flex-col" style={{ borderColor: `${accentColor}30` }}>
      {/* Header with live P&L */}
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
          {/* Live P&L badge */}
          <div className="flex flex-col items-end">
            <span className={`text-sm font-bold tabular-nums ${pnlPositive ? 'text-emerald-400' : 'text-red-400'}`}>
              {pnlPositive ? '+' : ''}{pnlPct.toFixed(1)}%
            </span>
            {pnlSol !== null && (
              <span className={`text-[10px] tabular-nums ${pnlPositive ? 'text-emerald-600' : 'text-red-600'}`}>
                {pnlPositive ? '+' : ''}{pnlSol.toFixed(4)} SOL
              </span>
            )}
          </div>
          {/* Live indicator dot */}
          <div
            className={`w-2 h-2 rounded-full flex-shrink-0 ${isStale ? 'bg-amber-500' : 'bg-emerald-500'}`}
            style={isStale ? {} : { animation: 'pulse 1.5s ease-in-out infinite' }}
            title={isStale ? `Stale ${live.ageSecs}s` : 'Live'}
          />
          {live.mcUsd && (
            <span className="text-[10px] text-zinc-500 tabular-nums hidden sm:block">
              MC ${(live.mcUsd / 1000).toFixed(0)}K
            </span>
          )}
          <a
            href={`https://solscan.io/token/${slot.mint}`}
            target="_blank"
            rel="noreferrer"
            className="text-zinc-600 hover:text-zinc-400 transition-colors text-xs"
            title="Solscan"
          >↗</a>
          <button
            onClick={onDismiss}
            className="w-5 h-5 flex items-center justify-center rounded text-zinc-600 hover:text-zinc-300 hover:bg-zinc-700/50 transition-all text-xs"
            title="Close chart"
          >✕</button>
        </div>
      </div>

      {/* DexScreener iframe — live chart, real-time data */}
      <iframe
        key={slot.mint}
        src={`https://dexscreener.com/solana/${slot.mint}?embed=1&theme=dark&trades=0&info=0`}
        className="w-full rounded-lg border-0"
        style={{ height: '340px' }}
        allow="clipboard-write"
        title={`Chart ${slot.mint}`}
      />
    </GlassCard>
  )
}

export default function LiveChartsRow() {
  const { positions } = useDashboardStore()
  const [slots, setSlots] = useState<ChartSlot[]>([])

  useEffect(() => {
    const openMints = new Set(Object.keys(positions))
    setSlots(prev => {
      const prevMints = prev.map(s => s.mint)
      const newSlots: ChartSlot[] = Array.from(openMints)
        .filter(m => !prevMints.includes(m))
        .map(m => ({
          mint: m,
          dex: positions[m].dex,
          pnl_pct: positions[m].pnl_pct,
          entry_price_sol: positions[m].entry_price_sol,
          entry_sol_spent: positions[m].entry_sol_spent,
          dismissed: false,
        }))
      const updated = prev
        .filter(s => openMints.has(s.mint))
        .map(s => ({
          ...s,
          dex: positions[s.mint]?.dex ?? s.dex,
          // Don't update pnl_pct from backend — live hook handles it
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
        {visible.map(slot => (
          <LiveChartCard key={slot.mint} slot={slot} onDismiss={() => dismiss(slot.mint)} />
        ))}
      </div>
    </div>
  )
}

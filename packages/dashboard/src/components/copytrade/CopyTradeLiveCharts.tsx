import { useState, useEffect } from 'react'
import { GlassCard } from '../common/GlassCard'

interface CopyPos {
  mint: string
  token_name: string
  wallet: string
  pnl_pct?: number | null
  entry_price?: number
  current_price?: number | null
}

interface Props {
  positions: CopyPos[]
}

export default function CopyTradeLiveCharts({ positions }: Props) {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())

  // Re-show charts when a new position arrives
  useEffect(() => {
    const currentMints = new Set(positions.map(p => p.mint))
    setDismissed(prev => {
      const next = new Set(prev)
      for (const m of Array.from(next)) {
        if (!currentMints.has(m)) next.delete(m) // auto-clean closed positions
      }
      return next
    })
  }, [positions])

  const visible = positions.filter(p => !dismissed.has(p.mint)).slice(0, 3)

  if (positions.length === 0) {
    return (
      <div className="col-span-12 rounded-xl border border-zinc-800/50 px-6 py-8 text-center"
        style={{ background: 'rgba(12,16,28,0.4)' }}>
        <div className="text-2xl mb-2">📡</div>
        <div className="text-zinc-500 text-sm">No open positions — charts will appear here when whales enter</div>
      </div>
    )
  }

  return (
    <div className="col-span-12 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {visible.map(pos => {
        const pnl = pos.pnl_pct ?? 0
        const pnlPos = pnl >= 0
        const isMoon = pnl >= 100
        const embedUrl = `https://dexscreener.com/solana/${pos.mint}?embed=1&theme=dark&trades=0&info=0`

        return (
          <GlassCard key={pos.mint} className="flex flex-col"
            style={{ borderColor: isMoon ? 'rgba(249,115,22,0.5)' : 'rgba(249,115,22,0.2)' }}>
            {/* Header */}
            <div className="flex items-center justify-between mb-2 flex-shrink-0">
              <div className="flex items-center gap-2 min-w-0">
                <div className="w-2 h-2 rounded-full bg-orange-400 animate-pulse flex-shrink-0" />
                <span className="font-semibold text-xs text-orange-300 truncate">
                  {pos.token_name.length > 16 ? pos.token_name.slice(0, 16) + '…' : pos.token_name}
                </span>
                <span className="text-[10px] px-1.5 py-0.5 rounded flex-shrink-0 font-medium"
                  style={{ background: 'rgba(249,115,22,0.15)', color: '#fb923c' }}>
                  {pos.wallet}
                </span>
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                {pos.pnl_pct != null && (
                  <span className={`text-sm font-bold tabular-nums ${pnlPos ? 'text-emerald-400' : 'text-red-400'}`}
                    style={isMoon ? { textShadow: '0 0 10px rgba(52,211,153,0.7)' } : undefined}>
                    {pnlPos ? '+' : ''}{pnl.toFixed(1)}%
                  </span>
                )}
                <a href={`https://solscan.io/token/${pos.mint}`} target="_blank" rel="noreferrer"
                  className="text-zinc-600 hover:text-zinc-400 transition-colors text-xs" title="Solscan">↗</a>
                <button
                  onClick={() => setDismissed(prev => new Set([...Array.from(prev), pos.mint]))}
                  className="w-5 h-5 flex items-center justify-center rounded text-zinc-600 hover:text-zinc-300 hover:bg-zinc-700/50 transition-all text-xs">
                  ✕
                </button>
              </div>
            </div>

            {/* Entry info */}
            {pos.entry_price != null && pos.entry_price > 0 && (
              <div className="flex items-center gap-3 mb-2 text-[10px] text-zinc-600 font-mono">
                <span>Entry: {pos.entry_price.toExponential(3)}</span>
                {pos.current_price != null && (
                  <span>Now: {pos.current_price.toExponential(3)}</span>
                )}
              </div>
            )}

            {/* DexScreener chart */}
            <iframe
              key={pos.mint}
              src={embedUrl}
              className="w-full rounded-lg border-0 flex-1"
              style={{ height: '320px' }}
              allow="clipboard-write"
              title={`Chart ${pos.token_name}`}
            />
          </GlassCard>
        )
      })}
    </div>
  )
}

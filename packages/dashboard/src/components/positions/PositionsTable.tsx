import { useDashboardStore } from '../../store/dashboard'
import { GlassCard } from '../common/GlassCard'
import { Badge } from '../common/Badge'
import { closePosition } from '../../api/client'
import { useLivePrice } from '../../hooks/useLivePrice'
import type { PositionData } from '../../types'

// Row with live price fetched directly from DexScreener every 3s
function LivePositionRow({
  pos,
  isSelected,
  onRowClick,
  onClose,
  connected,
}: {
  pos: PositionData
  isSelected: boolean
  onRowClick: () => void
  onClose: (e: React.MouseEvent) => void
  connected: boolean
}) {
  const live = useLivePrice(pos.mint, pos.entry_price_sol, pos.entry_sol_spent, 1500)

  const pnlPct  = live.pnlPct  ?? pos.pnl_pct
  const pnlSol  = live.pnlSol  ?? pos.unrealized_pnl_sol
  const curPrice = live.price  ?? pos.current_price_sol
  const isStale = live.ageSecs > 8

  const isBigWin  = pnlPct >= 100
  const isGoodWin = pnlPct >= 50 && pnlPct < 100
  const rowClass  = isSelected
    ? 'border-b border-zinc-800/50 bg-cyan-500/10 cursor-pointer'
    : isBigWin
    ? 'moonshot-row border-b border-zinc-800/50 cursor-pointer'
    : isGoodWin
    ? 'big-win-row border-b border-zinc-800/50 cursor-pointer'
    : 'border-b border-zinc-800/50 hover:bg-zinc-800/20 transition-colors cursor-pointer'

  const dexLabel   = pos.dex === 'pump_fun' ? 'pump' : pos.dex === 'pumpswap' ? 'pumpswap' : pos.dex === 'social_momentum' ? 'social' : pos.dex
  const dexVariant = pos.dex === 'pump_fun' ? 'blue' : 'amber'

  const src = pos.signal_source || ''
  const isLifecycle = src.startsWith('lifecycle')
  const strategyLabel = isLifecycle ? 'LIFECYCLE' : src ? src.toUpperCase() : 'MONSTER'
  const strategyStyle = { background: 'rgba(20,184,166,0.15)', color: '#2dd4bf', border: '1px solid rgba(20,184,166,0.4)' }

  return (
    <tr className={rowClass} onClick={onRowClick}>
      <td className="py-2 font-mono text-zinc-200">
        <div className="flex items-center gap-1.5">
          {/* Primary: GMGN — fastest chart + live trading */}
          <a
            href={`https://gmgn.ai/sol/token/${pos.mint}`}
            target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            className={`transition-colors underline-offset-2 hover:underline ${isSelected ? 'text-cyan-400' : 'text-zinc-200 hover:text-cyan-400'}`}
            title="Open in GMGN (fastest — use for manual closes)"
          >{pos.mint.slice(0, 6)}…{pos.mint.slice(-4)}</a>
          {/* Quick-close terminals */}
          <a href={`https://axiom.trade/t/${pos.mint}`} target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            className="text-orange-500 hover:text-orange-300 transition-colors text-[10px] font-bold"
            title="Open in Axiom">[ax]</a>
          <a href={`https://dexscreener.com/solana/${pos.mint}`} target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            className="text-zinc-600 hover:text-zinc-400 transition-colors text-[10px]"
            title="DexScreener (may lag)">[ds]</a>
          <a href={`https://solscan.io/token/${pos.mint}`} target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            className="text-zinc-600 hover:text-zinc-400 transition-colors text-[10px]">[sc]</a>
          {/* Live indicator */}
          <div
            className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${isStale ? 'bg-amber-500' : 'bg-emerald-500'}`}
            style={isStale ? {} : { animation: 'pulse 1.5s ease-in-out infinite' }}
            title={isStale ? `Stale ${live.ageSecs}s ago` : 'Live price'}
          />
        </div>
        {/* Strategy badge — clearly differentiates VELOCITY vs LIFECYCLE side-by-side */}
        <div className="flex items-center gap-1 mt-0.5">
          <span
            className="text-[9px] font-bold px-1.5 py-0.5 rounded-sm tracking-wide"
            style={strategyStyle}
          >
            {strategyLabel}
          </span>
          {pos.token_name && (
            <span className="text-[10px] text-zinc-500">{pos.token_name}</span>
          )}
        </div>
        {isSelected && <span className="text-cyan-500 text-[10px]">▶</span>}
        {isBigWin && <span className="text-amber-400">🚀</span>}
        {isGoodWin && <span className="text-amber-400">🔥</span>}
      </td>
      <td className="py-2"><Badge variant={dexVariant}>{dexLabel}</Badge></td>
      <td className="py-2 text-right font-mono text-zinc-300">{pos.entry_price_sol.toExponential(3)}</td>
      <td className={`py-2 text-right font-mono ${pnlPct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
        {curPrice.toExponential(3)}
      </td>
      <td className={`py-2 text-right font-semibold tabular-nums ${pnlPct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
        {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(1)}%
      </td>
      <td className={`py-2 text-right font-mono tabular-nums ${pnlSol >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
        {pnlSol >= 0 ? '+' : ''}{pnlSol.toFixed(4)}
      </td>
      <td className="py-2 text-right font-mono text-zinc-400">{pos.entry_sol_spent.toFixed(3)}</td>
      <td className="py-2 w-24"><PnlBar pos={{ ...pos, pnl_pct: pnlPct, current_price_sol: curPrice }} /></td>
      <td className="py-2 text-center">
        <div className="flex justify-center gap-0.5">
          {[pos.tp1_hit, pos.tp2_hit, pos.tp3_hit].map((hit, i) => (
            <div key={i} className={`w-1.5 h-1.5 rounded-full ${hit ? 'bg-emerald-400' : 'bg-zinc-700'}`} />
          ))}
        </div>
      </td>
      <td className="py-2 text-right font-mono text-zinc-500">{formatAge(pos.age_seconds)}</td>
      <td className="py-2 text-right">
        <button
          onClick={onClose}
          disabled={!connected}
          className="px-2 py-0.5 text-xs rounded bg-red-500/20 text-red-400 hover:bg-red-500/30 border border-red-500/30 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
        >Close</button>
      </td>
    </tr>
  )
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  return `${(seconds / 3600).toFixed(1)}h`
}

function PnlBar({ pos }: { pos: PositionData }) {
  const entry   = pos.entry_price_sol
  const current = pos.current_price_sol
  const sl      = pos.stop_loss_price
  const tp3     = pos.tp3_price

  if (!entry || !tp3 || !sl) return null

  const range = tp3 - sl
  const currentPct = range > 0 ? ((current - sl) / range) * 100 : 50
  const clampedPct = Math.max(0, Math.min(100, currentPct))

  return (
    <div className="relative h-1.5 bg-zinc-800 rounded-full overflow-visible my-0.5">
      {/* SL zone */}
      <div className="absolute left-0 top-0 h-full w-[8%] bg-red-900/60 rounded-l-full" />
      {/* TP zones */}
      <div className="absolute top-0 h-full bg-emerald-900/30 rounded-r-full"
        style={{ left: '33%', right: 0 }} />
      {/* Current price cursor */}
      <div
        className="absolute top-1/2 -translate-y-1/2 w-2 h-2 rounded-full border border-zinc-900 shadow-sm z-10 transition-all"
        style={{
          left: `${clampedPct}%`,
          backgroundColor: pos.pnl_pct >= 0 ? '#10b981' : '#ef4444',
          boxShadow: pos.pnl_pct >= 0
            ? '0 0 4px rgba(16,185,129,0.8)'
            : '0 0 4px rgba(239,68,68,0.8)',
        }}
      />
    </div>
  )
}

export default function PositionsTable() {
  const { positions, connected, selectedMint, setSelectedMint } = useDashboardStore()
  const rows = Object.values(positions)

  const handleClose = async (mint: string, e: React.MouseEvent) => {
    e.stopPropagation()
    if (!connected) return
    if (!window.confirm(`Close position for ${mint.slice(0, 8)}...?`)) return
    try {
      await closePosition(mint)
    } catch (err) {
      alert(`Close failed: ${err instanceof Error ? err.message : 'Server error — check bot logs'}`)
    }
  }

  const handleRowClick = (mint: string) => {
    setSelectedMint(selectedMint === mint ? null : mint)
  }

  return (
    <GlassCard className="overflow-hidden">
      <div className="flex items-center justify-between mb-4">
        <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">
          Open Positions
          {rows.length > 0 && (
            <span className="ml-2 bg-zinc-700 text-zinc-300 rounded-full px-2 py-0.5 text-xs">
              {rows.length}
            </span>
          )}
        </span>
      </div>

      {rows.length === 0 ? (
        <div className="text-center py-8 text-zinc-600 text-sm">No open positions</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-zinc-500 border-b border-zinc-800">
                <th className="text-left pb-2 font-medium">Token</th>
                <th className="text-left pb-2 font-medium">DEX</th>
                <th className="text-right pb-2 font-medium">Entry</th>
                <th className="text-right pb-2 font-medium">Current</th>
                <th className="text-right pb-2 font-medium">P&L %</th>
                <th className="text-right pb-2 font-medium">P&L SOL</th>
                <th className="text-right pb-2 font-medium">Invested</th>
                <th className="pb-2 font-medium">Progress</th>
                <th className="text-center pb-2 font-medium">TPs</th>
                <th className="text-right pb-2 font-medium">Age</th>
                <th className="text-right pb-2 font-medium">Action</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((pos) => (
                <LivePositionRow
                  key={pos.mint}
                  pos={pos}
                  isSelected={selectedMint === pos.mint}
                  onRowClick={() => handleRowClick(pos.mint)}
                  onClose={(e) => handleClose(pos.mint, e)}
                  connected={connected}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </GlassCard>
  )
}

import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'

async function closePosition(mint: string): Promise<void> {
  const res = await fetch(`/api/copy-trade/close/${mint}`, { method: 'POST' })
  if (!res.ok) throw new Error(`Close failed: ${res.status}`)
}

interface PartialExit {
  tp_level: string
  ts: number
  pnl_pct_at_exit: number
  fraction_sold: number
  sol_received: number
  sig?: string
}

interface CopyTradeOpenPos {
  mint: string
  token_name: string
  wallet: string
  entry_ts: number
  sol_spent: number
  entry_price?: number
  current_price?: number | null
  pnl_pct?: number | null
  mc_usd?: number | null
  tp1_hit?: boolean
  tp2_hit?: boolean
  locked_sol?: number
  remaining_fraction?: number
  narrative?: string
  moonbag_ceiling_pct?: number
  cp_l1_hit?: boolean
  cp_l2_hit?: boolean
  cp_l3_hit?: boolean
  peak_pnl_pct?: number
  ai_entry_verdict?: string
  partial_exits?: PartialExit[]
}

interface CopyTradeStats {
  balance: number
  open_count: number
  max_positions?: number
  open_positions: CopyTradeOpenPos[]
  net_pnl: number
  trades: number
  win_rate: number
  live_mode?: boolean
}

function formatAge(ts: number): string {
  const secs = Math.floor((Date.now() / 1000) - ts)
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`
  return `${(secs / 3600).toFixed(1)}h`
}

function formatMC(mc: number): string {
  if (mc >= 1_000_000) return `$${(mc / 1_000_000).toFixed(1)}M`
  if (mc >= 1_000)     return `$${(mc / 1_000).toFixed(0)}k`
  return `$${mc.toFixed(0)}`
}

// Low MC = below $150k. Stagnant warning shown in the UI so user knows Jarvis is watching it.
const LOW_MC_THRESHOLD = 150_000

// Copy-trade partial TP ladder tiers (must match live_config.py defaults).
const LADDER_TIERS: Array<{ pct: number; label: string }> = [
  { pct: 8,  label: 'L1' },
  { pct: 15, label: 'L2' },
  { pct: 30, label: 'L3' },
]

function LadderProgress({ pos }: { pos: CopyTradeOpenPos }) {
  const hits = [pos.cp_l1_hit, pos.cp_l2_hit, pos.cp_l3_hit]
  const pnl = pos.pnl_pct ?? 0
  // If any tier fired or we have partial_exits, render the ladder.
  const hasFired = hits.some(Boolean) || (pos.partial_exits?.length ?? 0) > 0
  const showPreview = pnl > -5 && pnl < 8  // getting close
  if (!hasFired && !showPreview) return null

  const nextTier = LADDER_TIERS.find((t, i) => !hits[i])
  return (
    <div className="flex items-center gap-1 mt-0.5" title="Partial TP ladder: L1 40% @+8% · L2 30% @+15% · L3 20% @+30% · 10% runner">
      {LADDER_TIERS.map((t, i) => {
        const fired = hits[i]
        const active = !fired && pnl >= (i === 0 ? 0 : LADDER_TIERS[i-1].pct)
        return (
          <div key={t.label} className="flex items-center gap-0.5">
            <div
              className="h-[3px] w-5 rounded-full transition-all duration-500"
              style={{
                background: fired
                  ? '#34d399'
                  : active
                    ? 'rgba(249,115,22,0.5)'
                    : 'rgba(63,63,70,0.5)',
                boxShadow: fired ? '0 0 6px rgba(52,211,153,0.7)' : 'none',
              }}
            />
            <span className={`text-[8px] font-mono ${fired ? 'text-emerald-400' : active ? 'text-orange-400' : 'text-zinc-700'}`}>
              {fired ? '✓' : `+${t.pct}`}
            </span>
          </div>
        )
      })}
      {nextTier && !hasFired && (
        <span className="text-[8px] text-zinc-600 ml-1 italic">
          next +{nextTier.pct}%
        </span>
      )}
    </div>
  )
}

interface CopyTradePositionsProps {
  selectedMint?: string | null
  onSelectMint?: (mint: string | null) => void
}

export default function CopyTradePositions({ selectedMint, onSelectMint }: CopyTradePositionsProps = {}) {
  const [stats, setStats] = useState<CopyTradeStats | null>(null)
  const [tick, setTick] = useState(0)
  const [closing, setClosing] = useState<string | null>(null)

  const handleClose = async (mint: string, name: string) => {
    if (closing) return
    setClosing(mint)
    try {
      await closePosition(mint)
      // Refresh immediately after close
      const res = await fetch('/api/copy-trade/stats')
      if (res.ok) setStats(await res.json())
    } catch (e) {
      console.error('Close failed', e)
    } finally {
      setClosing(null)
    }
  }

  // Poll stats every 2s for lightning-fast P&L updates
  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await fetch('/api/copy-trade/stats')
        if (res.ok) setStats(await res.json())
      } catch { /* ignore */ }
    }
    fetchStats()
    const interval = setInterval(fetchStats, 2_000)
    return () => clearInterval(interval)
  }, [])

  // Tick every second to keep age counters live
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [])

  const positions = stats?.open_positions ?? []
  const MAX_SLOTS = stats?.max_positions ?? 2

  return (
    <GlassCard className="overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">
            Copy Trade Positions
          </span>
          {positions.length > 0 && (
            <span className="bg-orange-500/20 text-orange-400 rounded-full px-2 py-0.5 text-xs font-bold">
              {positions.length}
            </span>
          )}
          <span className="text-[10px] text-zinc-600">
            ({positions.length}/{MAX_SLOTS} slots)
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px]">
          {stats && (
            <>
              <span className="text-zinc-500">
                {stats.live_mode ? 'Balance:' : 'Virtual:'} <span className="text-amber-400 font-mono">{stats.balance.toFixed(3)} SOL</span>
              </span>
              {stats.trades > 0 && (
                <span className="text-zinc-500">
                  P&L: <span className={`font-mono font-semibold ${stats.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {stats.net_pnl >= 0 ? '+' : ''}{stats.net_pnl.toFixed(4)} SOL
                  </span>
                </span>
              )}
            </>
          )}
          <span className="text-[9px] font-bold px-1.5 py-0.5 rounded animate-pulse"
            style={{ background: 'rgba(249,115,22,0.15)', color: '#f97316' }}>
            LIVE
          </span>
        </div>
      </div>

      {/* Slot visualization */}
      <div className="flex gap-1 mb-3">
        {Array.from({ length: MAX_SLOTS }).map((_, i) => (
          <div
            key={i}
            className="h-1 flex-1 rounded-full transition-all duration-500"
            style={{
              backgroundColor: i < positions.length
                ? '#f97316'
                : 'rgba(63,63,70,0.5)',
              boxShadow: i < positions.length ? '0 0 6px rgba(249,115,22,0.5)' : 'none',
            }}
          />
        ))}
      </div>

      {positions.length === 0 ? (
        <div className="text-center py-6 text-zinc-600 text-sm">
          <div className="text-2xl mb-2">👁</div>
          <div>Watching 6 wallets — waiting for entry signal…</div>
          <div className="text-[11px] mt-1 text-zinc-700">
            When a watched whale buys, we enter within 3 seconds
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-zinc-500 border-b border-zinc-800">
                <th className="text-left pb-2 font-medium">Token</th>
                <th className="text-left pb-2 font-medium">Following</th>
                <th className="text-right pb-2 font-medium">Size</th>
                <th className="text-right pb-2 font-medium">Entry</th>
                <th className="text-right pb-2 font-medium">Age</th>
                <th className="text-right pb-2 font-medium">P&amp;L</th>
                <th className="pb-2 w-8"></th>
              </tr>
            </thead>
            <tbody>
              {positions.map((pos) => {
                const ageStr = formatAge(pos.entry_ts)
                const hasPnl = pos.pnl_pct != null && pos.entry_price != null && pos.entry_price > 0
                const isLowMC = pos.mc_usd != null && pos.mc_usd < LOW_MC_THRESHOLD && pos.mc_usd > 0
                const ageSecs = (Date.now() / 1000) - pos.entry_ts
                const isStagnantWatch = isLowMC && ageSecs > 600
                const isMoonbag = pos.tp1_hit === true
                const hasLocked = (pos.locked_sol ?? 0) > 0
                const remainPct = Math.round((pos.remaining_fraction ?? 1.0) * 100)
                const isSelected = selectedMint === pos.mint
                return (
                  <tr key={pos.mint}
                    onClick={() => onSelectMint?.(isSelected ? null : pos.mint)}
                    className="border-b border-zinc-800/50 hover:bg-zinc-800/30 transition-colors cursor-pointer"
                    style={{
                      ...(isMoonbag ? { background: 'rgba(52,211,153,0.03)' } : {}),
                      ...(isSelected ? {
                        background: 'rgba(249,115,22,0.08)',
                        boxShadow: 'inset 3px 0 0 #f97316',
                      } : {}),
                    }}
                    title={isSelected ? 'Click to deselect' : 'Click to focus AI brains on this position'}>
                    <td className="py-2 font-mono text-zinc-200">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <div className={`w-1.5 h-1.5 rounded-full animate-pulse ${isMoonbag ? 'bg-emerald-400' : 'bg-orange-400'}`} />
                        <span className={`font-semibold ${isMoonbag ? 'text-emerald-300' : 'text-orange-300'}`}>
                          {pos.token_name.length > 14 ? pos.token_name.slice(0, 14) + '…' : pos.token_name}
                        </span>
                        {pos.mc_usd != null && pos.mc_usd > 0 && (
                          <span className={`text-[9px] font-mono px-1 py-0.5 rounded ${isLowMC ? 'text-amber-400 bg-amber-400/10' : 'text-zinc-500 bg-zinc-800'}`}>
                            {formatMC(pos.mc_usd)}
                          </span>
                        )}
                        {/* Moonbag stage badges */}
                        {pos.tp2_hit && (
                          <span className="text-[9px] px-1.5 py-0.5 rounded font-bold"
                            style={{ background: 'rgba(52,211,153,0.2)', color: '#34d399' }}
                            title="TP1 (+40%) and TP2 (+100%) both fired — running 20% moonbag">
                            💎 moonbag
                          </span>
                        )}
                        {pos.tp1_hit && !pos.tp2_hit && (
                          <span className="text-[9px] px-1.5 py-0.5 rounded font-bold animate-pulse"
                            style={{ background: 'rgba(249,115,22,0.2)', color: '#fb923c' }}
                            title="TP1 (+40%) fired — cost covered, running 40% free">
                            🎯 TP1 {remainPct}% left
                          </span>
                        )}
                        {isStagnantWatch && !isMoonbag && (
                          <span className="text-[9px] px-1 py-0.5 rounded animate-pulse"
                            style={{ background: 'rgba(251,191,36,0.15)', color: '#fbbf24' }}
                            title="Low MC — Jarvis watching for stagnation">
                            💤 watching
                          </span>
                        )}
                        {pos.ai_entry_verdict && (
                          <span className="text-[9px] px-1 py-0.5 rounded font-bold"
                            style={{
                              background: pos.ai_entry_verdict === 'RUNNER'
                                ? 'rgba(52,211,153,0.18)'
                                : pos.ai_entry_verdict === 'RUG_RISK'
                                  ? 'rgba(239,68,68,0.18)'
                                  : 'rgba(148,163,184,0.15)',
                              color: pos.ai_entry_verdict === 'RUNNER'
                                ? '#34d399'
                                : pos.ai_entry_verdict === 'RUG_RISK'
                                  ? '#f87171'
                                  : '#94a3b8',
                            }}
                            title={`Groq entry verdict: ${pos.ai_entry_verdict}`}>
                            🧠 {pos.ai_entry_verdict}
                          </span>
                        )}
                      </div>
                      <div className="text-zinc-600 text-[10px] font-mono mt-0.5 flex items-center gap-2">
                        <span>{pos.mint.slice(0, 8)}…</span>
                        {pos.narrative && pos.narrative !== 'unknown' && (
                          <span className="text-zinc-700 text-[9px] italic">{pos.narrative.replace('_', ' ')}</span>
                        )}
                      </div>
                    </td>
                    <td className="py-2">
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-medium"
                        style={{ background: 'rgba(249,115,22,0.15)', color: '#fb923c' }}>
                        {pos.wallet}
                      </span>
                    </td>
                    <td className="py-2 text-right font-mono text-zinc-300">
                      {pos.sol_spent.toFixed(2)} SOL
                    </td>
                    <td className="py-2 text-right font-mono text-zinc-500 text-[10px]">
                      {pos.entry_price && pos.entry_price > 0
                        ? pos.entry_price.toExponential(3)
                        : '—'}
                    </td>
                    <td className="py-2 text-right font-mono text-zinc-400">
                      {ageStr}
                    </td>
                    <td className="py-2 text-right">
                      {hasPnl ? (
                        <div className="flex flex-col items-end gap-0.5">
                          <span
                            className={`font-bold tabular-nums text-sm ${(pos.pnl_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
                            style={(pos.pnl_pct ?? 0) >= 100 ? { textShadow: '0 0 8px rgba(52,211,153,0.6)' } : undefined}
                          >
                            {(pos.pnl_pct ?? 0) >= 0 ? '+' : ''}{pos.pnl_pct?.toFixed(1)}%
                          </span>
                          {(pos.peak_pnl_pct ?? 0) > (pos.pnl_pct ?? 0) + 2 && (
                            <span className="text-zinc-500 text-[9px] font-mono"
                              title="Peak P&L this position reached">
                              peak +{(pos.peak_pnl_pct ?? 0).toFixed(1)}%
                            </span>
                          )}
                          <LadderProgress pos={pos} />
                          {hasLocked && (
                            <span className="text-emerald-600 text-[9px] font-mono"
                              title={`SOL locked via partial exits (${pos.partial_exits?.length ?? 0} tranches)`}>
                              +{(pos.locked_sol ?? 0).toFixed(4)} locked · {remainPct}% left
                            </span>
                          )}
                          {pos.current_price != null && (
                            <span className="text-zinc-600 text-[9px] font-mono">
                              {pos.current_price.toExponential(3)}
                            </span>
                          )}
                        </div>
                      ) : (
                        <span className="text-zinc-600 text-[10px] animate-pulse">fetching…</span>
                      )}
                    </td>
                    <td className="py-2 pl-2">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleClose(pos.mint, pos.token_name) }}
                        disabled={closing === pos.mint}
                        title="Close position now"
                        className="w-6 h-6 rounded flex items-center justify-center transition-all duration-150 disabled:opacity-40"
                        style={{
                          background: closing === pos.mint ? 'rgba(239,68,68,0.1)' : 'rgba(239,68,68,0.15)',
                          color: '#f87171',
                          border: '1px solid rgba(239,68,68,0.3)',
                        }}
                        onMouseEnter={e => (e.currentTarget.style.background = 'rgba(239,68,68,0.35)')}
                        onMouseLeave={e => (e.currentTarget.style.background = 'rgba(239,68,68,0.15)')}
                      >
                        {closing === pos.mint ? (
                          <span className="text-[8px] animate-spin">⟳</span>
                        ) : (
                          <span className="text-[10px] font-bold leading-none">✕</span>
                        )}
                      </button>
                    </td>
                  </tr>
                )
              })}
              {/* Empty slots */}
              {Array.from({ length: MAX_SLOTS - positions.length }).map((_, i) => (
                <tr key={`empty-${i}`} className="border-b border-zinc-800/30">
                  <td colSpan={7} className="py-2 text-center text-zinc-700 text-[10px] tracking-widest">
                    — slot available —
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </GlassCard>
  )
}

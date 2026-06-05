import { GlassCard } from '../common/GlassCard'

interface ClosedTrade {
  ts: number
  dt?: string
  mint: string
  token_name: string
  wallet: string
  reason: string
  sol_spent: number
  entry_price: number
  exit_price?: number
  pnl_sol: number
  pnl_pct: number
  hold_mins: number
  peak_pnl_pct?: number
  entry_lag_secs?: number | null
  pnl_source?: string
  sell_sig?: string
  buy_sig?: string
  // Moonbag fields
  tp1_hit?: boolean
  tp2_hit?: boolean
  locked_sol?: number
  remaining_fraction_at_close?: number
  narrative?: string
  partial_exits?: Array<{
    tp_level: string
    ts: number
    pnl_pct_at_exit: number
    fraction_sold: number
    sol_received: number
    sig?: string
  }>
}

interface Props {
  trades: ClosedTrade[]
  netPnl: number
  winRate: number
  wins: number
  total: number
}

function relTime(ts: number): string {
  const diff = Math.floor((Date.now() / 1000) - ts)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function reasonLabel(r: string): string {
  if (r.startsWith('wallet_exit')) return '🐋 whale exit'
  if (r.startsWith('stop_loss')) return '🛑 stop loss'
  if (r === 'manual_close') return '✋ manual'
  if (r.startsWith('moonbag_trail')) return '🌙 trail stop'
  if (r.startsWith('trail')) return '📈 trail stop'
  if (r.startsWith('narrative_ceiling')) return '🎯 ceiling'
  if (r.startsWith('health_mass_exit')) return '🚨 mass exit'
  if (r.startsWith('health_artificial_pump')) return '⚠️ pump signal'
  if (r.startsWith('health_vol_fade')) return '💤 vol fade'
  if (r.startsWith('health_holder_decline')) return '📉 holder drop'
  if (r.startsWith('stagnant')) return '💤 stagnant'
  if (r.startsWith('max_hold')) return '⏰ max hold'
  return r.replace(/_/g, ' ')
}

function stageLabel(t: ClosedTrade): { label: string; color: string; title: string } {
  if (t.tp2_hit)  return { label: 'TP1+TP2+bag', color: '#34d399', title: 'Full moonbag — TP1 at +40%, TP2 at +100%, then moonbag run' }
  if (t.tp1_hit)  return { label: 'TP1+bag',     color: '#fb923c', title: 'TP1 fired at +40%, moonbag ran until close signal' }
  return                  { label: 'full exit',   color: '#71717a', title: 'Closed without hitting TP1' }
}

export default function CopyTradeHistoryTable({ trades, netPnl, winRate, wins, total }: Props) {
  return (
    <GlassCard className="flex flex-col h-full min-h-0">
      {/* Header */}
      <div className="flex items-center justify-between mb-3 flex-shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">Copy Trade History</span>
          {total > 0 && (
            <span className={`text-xs font-mono font-semibold ${winRate >= 50 ? 'text-emerald-400' : 'text-amber-400'}`}>
              {winRate.toFixed(0)}% WR ({wins}/{total})
            </span>
          )}
        </div>
        {total > 0 && (
          <span className={`text-sm font-bold font-mono ${netPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {netPnl >= 0 ? '+' : ''}{netPnl.toFixed(4)} SOL
          </span>
        )}
      </div>

      {trades.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center gap-2 text-zinc-600">
          <div className="text-2xl">⏳</div>
          <div className="text-sm">No closed trades yet — positions will appear here when they close</div>
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[rgba(6,8,13,0.95)]">
              <tr className="text-zinc-500 border-b border-zinc-800">
                <th className="text-left pb-2 font-medium">Time</th>
                <th className="text-left pb-2 font-medium">Token</th>
                <th className="text-left pb-2 font-medium">Wallet</th>
                <th className="text-right pb-2 font-medium">Entry</th>
                <th className="text-right pb-2 font-medium">Exit</th>
                <th className="text-right pb-2 font-medium">P&L</th>
                <th className="text-right pb-2 font-medium">Locked</th>
                <th className="text-right pb-2 font-medium">Peak</th>
                <th className="text-right pb-2 font-medium">Lag</th>
                <th className="text-right pb-2 font-medium">Hold</th>
                <th className="text-left pb-2 font-medium">Stage</th>
                <th className="text-left pb-2 font-medium">Reason</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => {
                const win = t.pnl_pct >= 0
                const moon = t.pnl_pct >= 100
                const stage = stageLabel(t)
                const hasLocked = (t.locked_sol ?? 0) > 0
                return (
                  <tr key={`${t.mint}-${i}`}
                    className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors"
                    style={moon ? { background: 'rgba(249,115,22,0.05)' } : t.tp1_hit ? { background: 'rgba(52,211,153,0.02)' } : undefined}>
                    <td className="py-1.5 text-zinc-500 font-mono whitespace-nowrap">{relTime(t.ts)}</td>
                    <td className="py-1.5">
                      <div className="flex flex-col gap-0.5">
                        <a href={`https://solscan.io/token/${t.mint}`} target="_blank" rel="noreferrer"
                          className={`hover:opacity-80 transition-colors font-semibold ${t.tp1_hit ? 'text-emerald-300' : 'text-orange-300'}`}>
                          {moon && '🚀 '}
                          {t.token_name.length > 14 ? t.token_name.slice(0, 14) + '…' : t.token_name}
                        </a>
                        {t.narrative && t.narrative !== 'unknown' && (
                          <span className="text-[9px] text-zinc-700 italic">{t.narrative.replace('_', ' ')}</span>
                        )}
                      </div>
                    </td>
                    <td className="py-1.5">
                      {(() => {
                        const w = t.wallet || ''
                        const isVel = w.includes('velocity')
                        return (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium"
                            style={isVel
                              ? { background: 'rgba(249,115,22,0.25)', color: '#fb923c', border: '1px solid rgba(249,115,22,0.5)' }
                              : { background: 'rgba(20,184,166,0.12)', color: '#2dd4bf', border: '1px solid rgba(20,184,166,0.3)' }
                            }>
                            {isVel ? '⚡ ' : ''}{w.replace('monster/', '')}
                          </span>
                        )
                      })()}
                    </td>
                    <td className="py-1.5 text-right font-mono text-zinc-500 text-[10px]">
                      {t.entry_price > 0 ? t.entry_price.toExponential(3) : '—'}
                    </td>
                    <td className="py-1.5 text-right font-mono text-zinc-500 text-[10px]">
                      {t.exit_price && t.exit_price > 0 ? t.exit_price.toExponential(3) : '—'}
                    </td>
                    <td className={`py-1.5 text-right font-mono font-bold ${win ? 'text-emerald-400' : 'text-red-400'}`}>
                      {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct.toFixed(1)}%
                      <div className={`text-[9px] font-normal ${win ? 'text-emerald-600' : 'text-red-600'}`}>
                        {t.pnl_sol >= 0 ? '+' : ''}{t.pnl_sol.toFixed(4)} SOL
                      </div>
                      {t.pnl_source === 'onchain_sell_sig' && (
                        <div className="text-[8px] text-emerald-700 font-mono">⛓ verified</div>
                      )}
                    </td>
                    <td className="py-1.5 text-right font-mono text-[10px]">
                      {hasLocked
                        ? <span className="text-emerald-600"
                            title={`${(t.partial_exits ?? []).length} partial exit(s) locked profits early`}>
                            +{(t.locked_sol ?? 0).toFixed(4)}
                          </span>
                        : <span className="text-zinc-700">—</span>}
                    </td>
                    <td className="py-1.5 text-right font-mono text-amber-500 text-[10px]">
                      {t.peak_pnl_pct != null && t.peak_pnl_pct > 0
                        ? `+${t.peak_pnl_pct.toFixed(1)}%`
                        : '—'}
                    </td>
                    <td className="py-1.5 text-right font-mono text-zinc-500 text-[10px]">
                      {t.entry_lag_secs != null
                        ? <span title="Seconds from whale buy to our confirmed buy block"
                            className={t.entry_lag_secs <= 5 ? 'text-emerald-500' : t.entry_lag_secs <= 15 ? 'text-amber-500' : 'text-red-400'}>
                            {t.entry_lag_secs.toFixed(0)}s
                          </span>
                        : <span className="text-zinc-700">—</span>}
                    </td>
                    <td className="py-1.5 text-right font-mono text-zinc-500">
                      {t.hold_mins < 60
                        ? `${t.hold_mins.toFixed(0)}m`
                        : `${(t.hold_mins / 60).toFixed(1)}h`}
                    </td>
                    <td className="py-1.5 whitespace-nowrap">
                      <span className="text-[9px] px-1.5 py-0.5 rounded font-medium"
                        style={{ color: stage.color, background: `${stage.color}18` }}
                        title={stage.title}>
                        {stage.label}
                      </span>
                    </td>
                    <td className="py-1.5 text-zinc-500 whitespace-nowrap">
                      {reasonLabel(t.reason)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </GlassCard>
  )
}

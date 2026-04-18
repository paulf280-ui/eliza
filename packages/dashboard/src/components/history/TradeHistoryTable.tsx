import { useDashboardStore } from '../../store/dashboard'
import { GlassCard } from '../common/GlassCard'
import { Badge } from '../common/Badge'

function relativeTime(ts: number): string {
  const diff = Math.floor((Date.now() / 1000) - ts)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

export default function TradeHistoryTable() {
  const { tradeHistory } = useDashboardStore()

  // Download report handler
  const downloadReport = async () => {
    try {
      const res = await fetch('/api/report')
      const data = await res.json()
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `traderbot-report-${new Date().toISOString().slice(0, 10)}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      console.error('Failed to download report')
    }
  }

  const sells = tradeHistory.filter(t => t.side === 'sell' && t.pnl_sol !== null)
  const wins = sells.filter(t => (t.pnl_sol ?? 0) > 0).length
  const winRate = sells.length > 0 ? Math.round((wins / sells.length) * 100) : 0

  return (
    <GlassCard className="flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between mb-4 flex-shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">Trade History</span>
          {sells.length > 0 && (
            <span className={`text-xs font-mono ${winRate >= 50 ? 'text-emerald-400' : 'text-red-400'}`}>
              {winRate}% win rate ({wins}/{sells.length})
            </span>
          )}
        </div>
        <button
          onClick={downloadReport}
          className="px-3 py-1 rounded text-xs font-semibold bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30 border border-cyan-500/30 transition-all"
        >
          Download Report
        </button>
      </div>

      {tradeHistory.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-zinc-600 text-sm">No trades yet</div>
      ) : (
        <div className="flex-1 min-h-0 overflow-x-auto overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[rgba(6,8,13,0.95)]">
              <tr className="text-zinc-500 border-b border-zinc-800">
                <th className="text-left pb-2 font-medium">Time</th>
                <th className="text-left pb-2 font-medium">Token</th>
                <th className="text-left pb-2 font-medium">DEX</th>
                <th className="text-center pb-2 font-medium">Side</th>
                <th className="text-right pb-2 font-medium">SOL</th>
                <th className="text-right pb-2 font-medium">P&L %</th>
                <th className="text-right pb-2 font-medium">P&L SOL</th>
                <th className="text-left pb-2 font-medium">Reason</th>
                <th className="text-left pb-2 font-medium">TX</th>
              </tr>
            </thead>
            <tbody>
              {tradeHistory.map((t) => {
                const pnl = t.pnl_pct ?? 0
                const isMoonshot = t.side === 'sell' && pnl >= 100
                const isBigWin = t.side === 'sell' && pnl >= 50 && pnl < 100
                const rowClass = isMoonshot
                  ? 'big-win-trade border-b border-zinc-800/40'
                  : isBigWin
                  ? 'big-win-trade border-b border-zinc-800/40'
                  : 'border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors'
                const dexLabel = t.dex === 'pump_fun' ? 'pump' : t.dex === 'pumpswap' ? 'pswap' : t.dex === 'social_momentum' ? 'social' : t.dex ?? 'ray'
                const dexVariant = t.dex === 'pump_fun' ? 'blue' : 'amber'
                return (
                <tr key={t.id} className={rowClass}>
                  <td className="py-1.5 text-zinc-500 font-mono">{relativeTime(t.timestamp)}</td>
                  <td className="py-1.5 font-mono text-zinc-300">
                    <a href={`https://solscan.io/token/${t.mint}`} target="_blank" rel="noreferrer"
                      className="hover:text-cyan-400 transition-colors">
                      {t.mint.slice(0, 6)}…{t.mint.slice(-4)}
                    </a>
                    {isMoonshot && <span className="ml-1 text-amber-400">🚀</span>}
                    {isBigWin && <span className="ml-1 text-amber-400">🔥</span>}
                  </td>
                  <td className="py-1.5">
                    <Badge variant={dexVariant}>
                      {dexLabel}
                    </Badge>
                  </td>
                  <td className="py-1.5 text-center">
                    <span className={`font-semibold uppercase text-xs ${t.side === 'buy' ? 'text-cyan-400' : (t.pnl_sol ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {t.side}
                    </span>
                  </td>
                  <td className="py-1.5 text-right font-mono text-zinc-300">{t.sol_amount.toFixed(4)}</td>
                  <td className={`py-1.5 text-right font-mono font-semibold ${
                    t.pnl_pct === null ? 'text-zinc-500' : t.pnl_pct >= 0 ? 'text-emerald-400' : 'text-red-400'
                  }`}>
                    {t.pnl_pct !== null ? `${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(1)}%` : '—'}
                  </td>
                  <td className={`py-1.5 text-right font-mono ${
                    t.pnl_sol === null ? 'text-zinc-500' : t.pnl_sol >= 0 ? 'text-emerald-400' : 'text-red-400'
                  }`}>
                    {t.pnl_sol !== null ? `${t.pnl_sol >= 0 ? '+' : ''}${t.pnl_sol.toFixed(4)}` : '—'}
                  </td>
                  <td className="py-1.5">
                    <span className="text-zinc-500 text-xs">
                      {t.reason.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="py-1.5">
                    {t.signature && !t.signature.startsWith('PAPER_') ? (
                      <a href={`https://solscan.io/tx/${t.signature}`} target="_blank" rel="noreferrer"
                        className="text-zinc-600 hover:text-cyan-400 transition-colors font-mono">
                        {t.signature.slice(0, 8)}…
                      </a>
                    ) : (
                      <span className="text-zinc-700 font-mono text-xs">{t.signature?.slice(0, 12) || '—'}</span>
                    )}
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

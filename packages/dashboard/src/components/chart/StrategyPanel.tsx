import { useMemo, useEffect, useState } from 'react'
import { useDashboardStore } from '../../store/dashboard'
import type { TradeRecord } from '../../types'

interface StrategyStats {
  label: string
  color: string
  accent: string
  trades: number
  wins: number
  losses: number
  winRate: number
  netPnl: number
  avgWin: number
  avgLoss: number
  bestTrade: number
  extra?: string  // optional badge text (e.g. "PAPER")
}

interface CopyTradeStats {
  trades: number
  wins: number
  losses: number
  win_rate: number
  net_pnl: number
  avg_win: number
  avg_loss: number
  best_trade: number
  balance: number
  open_count: number
  watched_wallets: string[]
}

function computeStats(trades: TradeRecord[], dexes: string[], label: string, color: string, accent: string): StrategyStats {
  const sells = trades.filter(t => t.side === 'sell' && dexes.includes(t.dex) && t.pnl_sol != null)
  const wins = sells.filter(t => (t.pnl_sol ?? 0) > 0)
  const losses = sells.filter(t => (t.pnl_sol ?? 0) <= 0)
  const netPnl = sells.reduce((s, t) => s + (t.pnl_sol ?? 0), 0)
  const avgWin = wins.length > 0 ? wins.reduce((s, t) => s + (t.pnl_pct ?? 0), 0) / wins.length : 0
  const avgLoss = losses.length > 0 ? losses.reduce((s, t) => s + (t.pnl_pct ?? 0), 0) / losses.length : 0
  const bestTrade = sells.length > 0 ? Math.max(...sells.map(t => t.pnl_pct ?? 0)) : 0
  return {
    label, color, accent,
    trades: sells.length,
    wins: wins.length,
    losses: losses.length,
    winRate: sells.length > 0 ? (wins.length / sells.length) * 100 : 0,
    netPnl,
    avgWin,
    avgLoss,
    bestTrade,
  }
}

function StatRow({ label, value, valueClass = 'text-zinc-300' }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex items-center justify-between py-0.5">
      <span className="text-zinc-600 text-[11px]">{label}</span>
      <span className={`text-[11px] font-mono font-medium ${valueClass}`}>{value}</span>
    </div>
  )
}

function StrategyCard({ s }: { s: StrategyStats }) {
  const wrColor = s.winRate >= 50 ? 'text-emerald-400' : s.winRate >= 30 ? 'text-amber-400' : 'text-red-400'
  const pnlColor = s.netPnl >= 0 ? 'text-emerald-400' : 'text-red-400'

  return (
    <div
      className="rounded-lg p-3 border flex-1"
      style={{ background: 'rgba(12,16,28,0.6)', borderColor: `${s.accent}22` }}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full" style={{ backgroundColor: s.color }} />
          <span className="text-xs font-bold text-zinc-200 uppercase tracking-widest">{s.label}</span>
          {s.extra && (
            <span className="text-[9px] font-bold px-1 py-0.5 rounded"
              style={{ background: `${s.color}22`, color: s.color }}>
              {s.extra}
            </span>
          )}
        </div>
        {s.trades > 0 && (
          <span className={`text-xs font-bold tabular-nums ${wrColor}`}>
            {s.winRate.toFixed(0)}% WR
          </span>
        )}
      </div>

      {s.trades === 0 ? (
        <div className="text-center py-3 text-zinc-700 text-[11px]">No closed trades yet</div>
      ) : (
        <>
          <div className="relative h-1 bg-zinc-800 rounded-full mb-3 overflow-hidden">
            <div
              className="absolute left-0 top-0 h-full rounded-full transition-all duration-500"
              style={{ width: `${s.winRate}%`, backgroundColor: s.color }}
            />
          </div>
          <div className="space-y-0">
            <StatRow label="Closed trades" value={`${s.trades} (${s.wins}W / ${s.losses}L)`} />
            <StatRow
              label="Net P&L"
              value={`${s.netPnl >= 0 ? '+' : ''}${s.netPnl.toFixed(4)} SOL`}
              valueClass={pnlColor}
            />
            <StatRow
              label="Avg win"
              value={s.wins > 0 ? `+${s.avgWin.toFixed(1)}%` : '—'}
              valueClass="text-emerald-400"
            />
            <StatRow
              label="Avg loss"
              value={s.losses > 0 ? `${s.avgLoss.toFixed(1)}%` : '—'}
              valueClass="text-red-400"
            />
            <StatRow
              label="Best trade"
              value={s.bestTrade > 0 ? `+${s.bestTrade.toFixed(1)}%` : '—'}
              valueClass="text-amber-400"
            />
          </div>
        </>
      )}
    </div>
  )
}

function CopyTradeCard({ stats }: { stats: CopyTradeStats | null }) {
  const color  = '#f97316'  // orange — distinct from other strategies
  const accent = '#f97316'
  const wrColor = stats && stats.win_rate >= 50 ? 'text-emerald-400'
                : stats && stats.win_rate >= 30 ? 'text-amber-400' : 'text-red-400'
  const pnlColor = stats && stats.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'

  return (
    <div
      className="rounded-lg p-3 border flex-1"
      style={{ background: 'rgba(12,16,28,0.6)', borderColor: `${accent}22` }}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full" style={{ backgroundColor: color }} />
          <span className="text-xs font-bold text-zinc-200 uppercase tracking-widest">Copy Trade</span>
          <span className="text-[9px] font-bold px-1 py-0.5 rounded animate-pulse"
            style={{ background: `${color}22`, color }}>
            PAPER
          </span>
        </div>
        {stats && stats.trades > 0 && (
          <span className={`text-xs font-bold tabular-nums ${wrColor}`}>
            {stats.win_rate.toFixed(0)}% WR
          </span>
        )}
      </div>

      {!stats || stats.trades === 0 ? (
        <>
          <div className="text-center py-1 text-zinc-700 text-[11px]">No closed trades yet</div>
          {stats && (
            <div className="mt-1 space-y-0.5">
              <StatRow
                label="Virtual balance"
                value={`${stats.balance.toFixed(3)} SOL`}
                valueClass="text-amber-400"
              />
              <StatRow
                label="Open positions"
                value={String(stats.open_count)}
              />
              <StatRow
                label="Watching"
                value={`${stats.watched_wallets?.length ?? 5} wallets`}
              />
            </div>
          )}
        </>
      ) : (
        <>
          <div className="relative h-1 bg-zinc-800 rounded-full mb-3 overflow-hidden">
            <div
              className="absolute left-0 top-0 h-full rounded-full transition-all duration-500"
              style={{ width: `${stats.win_rate}%`, backgroundColor: color }}
            />
          </div>
          <div className="space-y-0">
            <StatRow label="Closed trades" value={`${stats.trades} (${stats.wins}W / ${stats.losses}L)`} />
            <StatRow
              label="Net P&L (paper)"
              value={`${stats.net_pnl >= 0 ? '+' : ''}${stats.net_pnl.toFixed(4)} SOL`}
              valueClass={pnlColor}
            />
            <StatRow
              label="Avg win"
              value={stats.wins > 0 ? `+${stats.avg_win.toFixed(1)}%` : '—'}
              valueClass="text-emerald-400"
            />
            <StatRow
              label="Avg loss"
              value={stats.losses > 0 ? `${stats.avg_loss.toFixed(1)}%` : '—'}
              valueClass="text-red-400"
            />
            <StatRow
              label="Best trade"
              value={stats.best_trade > 0 ? `+${stats.best_trade.toFixed(1)}%` : '—'}
              valueClass="text-amber-400"
            />
            <StatRow
              label="Virtual balance"
              value={`${stats.balance.toFixed(3)} SOL`}
              valueClass="text-zinc-400"
            />
          </div>
        </>
      )}
    </div>
  )
}

export default function StrategyPanel() {
  const { tradeHistory } = useDashboardStore()
  const [copyTradeStats, setCopyTradeStats] = useState<CopyTradeStats | null>(null)

  // Poll copy-trade paper stats every 15 seconds
  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await fetch('/api/copy-trade/stats')
        if (res.ok) setCopyTradeStats(await res.json())
      } catch { /* ignore */ }
    }
    fetchStats()
    const interval = setInterval(fetchStats, 15_000)
    return () => clearInterval(interval)
  }, [])

  const strategies = useMemo(() => [
    computeStats(tradeHistory, ['pumpswap'],  'PumpSwap', '#10b981', '#10b981'),
    computeStats(tradeHistory, ['raydium'],   'Raydium',  '#818cf8', '#818cf8'),
    computeStats(tradeHistory, ['pump_fun'],  'Pump.fun', '#22d3ee', '#22d3ee'),
  ], [tradeHistory])

  const totalTrades = strategies.reduce((s, x) => s + x.trades, 0)
    + (copyTradeStats?.trades ?? 0)
  const totalWins   = strategies.reduce((s, x) => s + x.wins, 0)
    + (copyTradeStats?.wins ?? 0)
  const totalPnl    = strategies.reduce((s, x) => s + x.netPnl, 0)
    + (copyTradeStats?.net_pnl ?? 0)

  return (
    <div className="flex flex-col h-full gap-3">
      {/* Overall summary bar */}
      <div className="flex items-center justify-between px-1">
        <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">Strategy Performance</span>
        {totalTrades > 0 && (
          <div className="flex items-center gap-3">
            <span className="text-[11px] text-zinc-500">
              {totalTrades} trades · {totalTrades > 0 ? ((totalWins / totalTrades) * 100).toFixed(0) : 0}% overall WR
            </span>
            <span className={`text-[11px] font-mono font-semibold ${totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(4)} SOL
            </span>
          </div>
        )}
      </div>

      {/* Strategy cards — 2×2 grid: top row live, bottom row Copy Trade + Pump.fun */}
      <div className="grid grid-cols-2 gap-2 flex-1">
        {strategies.slice(0, 2).map(s => <StrategyCard key={s.label} s={s} />)}
        <CopyTradeCard stats={copyTradeStats} />
        <StrategyCard key={strategies[2].label} s={strategies[2]} />
      </div>

      <p className="text-[10px] text-zinc-700 text-center">
        Copy Trade runs as a 48h paper test alongside live strategies
      </p>
    </div>
  )
}

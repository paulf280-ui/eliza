import { useEffect, useState, useCallback } from 'react'
import { DashboardLayout } from './components/layout/DashboardLayout'
import BotChat from './components/chat/BotChat'
import ActivityFeed from './components/activity/ActivityFeed'
import { useWebSocket } from './hooks/useWebSocket'
import { useDashboardStore } from './store/dashboard'
import { fetchStatus } from './api/client'
import PositionsTable from './components/positions/PositionsTable'
import CopyTradeSummaryBar from './components/copytrade/CopyTradeSummaryBar'
import TradeHistoryTable from './components/copytrade/CopyTradeHistoryTable'
import BrainPanel from './components/brains/BrainPanel'
import PositionConfigPanel from './components/diagnostics/PositionConfigPanel'
import CreatorAlphaPnLPanel from './components/diagnostics/CreatorAlphaPnLPanel'
import PhantomTerminalPanel from './components/phantom/PhantomTerminalPanel'

interface MonsterStats {
  balance: number
  net_pnl: number
  trades: number
  wins: number
  losses: number
  win_rate: number
  open_count: number
  max_positions?: number
  trade_size?: number
  live_mode?: boolean
  paused?: boolean
  open_positions: Array<{
    mint: string
    token_name: string
    wallet: string
    entry_ts: number
    sol_spent: number
    entry_price?: number
    current_price?: number | null
    pnl_pct?: number | null
    mc_usd?: number | null
    narrative?: string
  }>
  recent_trades: Array<{
    ts: number
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
    narrative?: string
  }>
  signals_today: number
  copy_trade_enabled?: boolean
  monster_paper_only?: boolean
}

function ZoneHeader({ label, hint }: { label: string; hint?: string }) {
  return (
    <div className="col-span-12 flex items-center gap-3 mt-2 mb-[-4px]">
      <div className="h-px flex-1 bg-gradient-to-r from-transparent via-zinc-700 to-transparent" />
      <span className="text-[10px] font-bold text-zinc-400 uppercase tracking-[0.25em]">
        {label}
      </span>
      {hint && <span className="text-[10px] text-zinc-600 italic">{hint}</span>}
      <div className="h-px flex-1 bg-gradient-to-r from-transparent via-zinc-700 to-transparent" />
    </div>
  )
}

export default function App() {
  const { sendCommand } = useWebSocket()
  const { setInitialState } = useDashboardStore()
  const wallet = useDashboardStore(s => s.wallet)
  const config = useDashboardStore(s => s.config)
  const [dataLoaded, setDataLoaded] = useState(false)
  const [stats, setStats] = useState<MonsterStats | null>(null)
  const [pauseLoading, setPauseLoading] = useState(false)

  const togglePause = useCallback(async () => {
    if (pauseLoading) return
    setPauseLoading(true)
    try {
      const newPaused = !stats?.paused
      await fetch('/api/copy-trade/pause', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paused: newPaused }),
      })
      const res = await fetch('/api/copy-trade/stats')
      if (res.ok) setStats(await res.json())
    } catch { /* ignore */ }
    finally { setPauseLoading(false) }
  }, [stats?.paused, pauseLoading])

  useEffect(() => {
    fetchStatus()
      .then(data => { setInitialState(data); setDataLoaded(true) })
      .catch(() => setDataLoaded(true))
    const id = setInterval(() =>
      fetchStatus().then(data => setInitialState(data)).catch(() => {}), 10000)
    return () => clearInterval(id)
  }, [setInitialState])

  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch('/api/copy-trade/stats')
      if (res.ok) setStats(await res.json())
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    fetchStats()
    const id = setInterval(fetchStats, 2000)
    return () => clearInterval(id)
  }, [fetchStats])

  if (!dataLoaded) {
    return (
      <div className="min-h-screen bg-[#06080d] flex flex-col items-center justify-center gap-6">
        <div className="flex items-center justify-center w-20 h-20 rounded-2xl text-2xl font-black"
          style={{ background: 'linear-gradient(135deg, #f97316, #fb923c)', color: '#0c1a2e' }}>
          ⚡
        </div>
        <div className="flex flex-col items-center gap-2">
          <span className="text-2xl font-bold text-slate-200 tracking-tight font-mono">J.A.R.V.I.S.</span>
          <span className="text-sm text-zinc-500 tracking-widest uppercase">Command Centre · Connecting…</span>
        </div>
        <div className="flex gap-1.5">
          {[0, 1, 2].map(i => (
            <div key={i} className="w-2 h-2 rounded-full bg-orange-400"
              style={{ animation: `pulse 1.2s ease-in-out ${i * 0.2}s infinite` }} />
          ))}
        </div>
      </div>
    )
  }

  const positions = stats?.open_positions ?? []
  const trades = stats?.recent_trades ?? []

  return (
    <DashboardLayout>

      {/* ── Summary bar: balance · P&L · WR · LIVE badge · pause ─────── */}
      {stats && (
        <CopyTradeSummaryBar
          balance={stats.balance}
          netPnl={stats.net_pnl}
          trades={stats.trades}
          wins={stats.wins}
          openCount={stats.open_count}
          maxSlots={Number(config?.monster_max_concurrent ?? 1)}
          signalsToday={stats.signals_today ?? 0}
          watchedWallets={[]}
          tradeSize={Number(config?.monster_default_size_sol ?? 0.5)}
          liveMode={stats.live_mode}
          paperMode={stats.monster_paper_only}
          paused={stats.paused}
          pauseLoading={pauseLoading}
          onTogglePause={togglePause}
        />
      )}

      {/* ── OPEN POSITIONS ───────────────────────────────────────────── */}
      <ZoneHeader label="Open Positions" hint="lifecycle_quiet · graduation snipe · 0.5 SOL × 1 slot · +20% TP · −25% floor" />

      <div className="col-span-12">
        <PositionsTable />
      </div>

      {/* ── AI BRAIN + COMMAND ────────────────────────────────────────── */}
      <ZoneHeader label="Intelligence" hint="Groq 30s → Gemini 2m → Claude 5m · candle pattern learning · every decision logged" />

      <div className="col-span-12 lg:col-span-8">
        <BrainPanel mint={positions[0]?.mint} />
      </div>
      <div className="col-span-12 lg:col-span-4 h-[460px]">
        <BotChat sendCommand={sendCommand} />
      </div>

      {/* ── TRADE HISTORY + ACTIVITY ──────────────────────────────────── */}
      <ZoneHeader label="Performance" hint="closed trades · win rate · P&L history" />

      <div className="col-span-12 lg:col-span-8 h-[420px]">
        <TradeHistoryTable
          trades={trades}
          netPnl={stats?.net_pnl ?? 0}
          winRate={stats?.win_rate ?? 0}
          wins={stats?.wins ?? 0}
          total={stats?.trades ?? 0}
        />
      </div>
      <div className="col-span-12 lg:col-span-4 h-[420px] overflow-hidden">
        <ActivityFeed />
      </div>

      {/* ── STRATEGY ANALYTICS ───────────────────────────────────────── */}
      <ZoneHeader label="Strategy Analytics" hint="lifecycle_quiet + creator-alpha · source breakdown · entry quality" />

      <div className="col-span-12 h-[400px]">
        <CreatorAlphaPnLPanel />
      </div>

      {/* ── CONTROLS + TERMINAL ──────────────────────────────────────── */}
      <ZoneHeader label="Controls" hint="live position-sizing · no restart required" />

      <div className="col-span-12 lg:col-span-5">
        <PositionConfigPanel
          solBalance={wallet?.sol_balance ?? 0}
          currentMaxConcurrent={Number(config?.monster_max_concurrent ?? 1)}
          currentTradeSize={Number(config?.monster_default_size_sol ?? 0.5)}
          onApplied={() => fetchStatus().then(setInitialState).catch(() => {})}
        />
      </div>

      <div className="col-span-12 h-[480px]">
        <PhantomTerminalPanel />
      </div>

    </DashboardLayout>
  )
}

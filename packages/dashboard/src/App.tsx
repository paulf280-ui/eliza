import { useEffect, useState, useCallback } from 'react'
import { DashboardLayout } from './components/layout/DashboardLayout'
import BotChat from './components/chat/BotChat'
import ActivityFeed from './components/activity/ActivityFeed'
import { useWebSocket } from './hooks/useWebSocket'
import { useDashboardStore } from './store/dashboard'
import { fetchStatus } from './api/client'
import CopyTradePositions from './components/positions/CopyTradePositions'
import PositionsTable from './components/positions/PositionsTable'
import CopyTradeSummaryBar from './components/copytrade/CopyTradeSummaryBar'
import CopyTradeLiveCharts from './components/copytrade/CopyTradeLiveCharts'
import CopyTradeHistoryTable from './components/copytrade/CopyTradeHistoryTable'
import WalletPromotionPanel from './components/wallets/WalletPromotionPanel'
import BrainPanel from './components/brains/BrainPanel'
import PositionConfigPanel from './components/diagnostics/PositionConfigPanel'
import CreatorAlphaPnLPanel from './components/diagnostics/CreatorAlphaPnLPanel'
import PhantomTerminalPanel from './components/phantom/PhantomTerminalPanel'

interface CopyTradeStats {
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
    tp1_hit?: boolean
    tp2_hit?: boolean
    locked_sol?: number
    remaining_fraction?: number
    narrative?: string
    moonbag_ceiling_pct?: number
    ai_entry_verdict?: string
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
    entry_lag_secs?: number | null
    pnl_source?: string
    tp1_hit?: boolean
    tp2_hit?: boolean
    locked_sol?: number
    narrative?: string
  }>
  watched_wallets: string[]
  signals_today: number
  copy_trade_enabled?: boolean
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
  const [ctStats, setCtStats] = useState<CopyTradeStats | null>(null)
  const [pauseLoading, setPauseLoading] = useState(false)
  const [selectedMint, setSelectedMint] = useState<string | null>(null)

  const togglePause = useCallback(async () => {
    if (pauseLoading) return
    setPauseLoading(true)
    try {
      const newPaused = !ctStats?.paused
      await fetch('/api/copy-trade/pause', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paused: newPaused }),
      })
      const res = await fetch('/api/copy-trade/stats')
      if (res.ok) setCtStats(await res.json())
    } catch { /* ignore */ }
    finally { setPauseLoading(false) }
  }, [ctStats?.paused, pauseLoading])

  useEffect(() => {
    fetchStatus()
      .then(data => { setInitialState(data); setDataLoaded(true) })
      .catch(() => setDataLoaded(true))
    const id = setInterval(() =>
      fetchStatus().then(data => setInitialState(data)).catch(() => {}), 10000)
    return () => clearInterval(id)
  }, [setInitialState])

  const fetchCT = useCallback(async () => {
    try {
      const res = await fetch('/api/copy-trade/stats')
      if (res.ok) setCtStats(await res.json())
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    fetchCT()
    const id = setInterval(fetchCT, 2000)
    return () => clearInterval(id)
  }, [fetchCT])

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

  const positions = ctStats?.open_positions ?? []
  const trades = ctStats?.recent_trades ?? []
  const copyTradeEnabled = ctStats?.copy_trade_enabled ?? false

  return (
    <DashboardLayout>

      {/* ── Row 0: Status banner + pause button (copy-trade only) ───── */}
      {copyTradeEnabled && <div className="col-span-12 flex items-center gap-3">
        {ctStats?.live_mode ? (
          <div className="flex-1 bg-red-500/10 border border-red-500/40 rounded-lg px-4 py-2 flex items-center gap-3">
            <div className="w-2 h-2 rounded-full bg-red-400 animate-pulse" />
            <span className="text-red-300 text-sm font-semibold tracking-wide">
              LIVE TRADING — Real funds active
            </span>
            <span className="text-zinc-500 text-xs">
              · Watching {ctStats?.watched_wallets?.length ?? 0} wallets
              · {ctStats?.open_count ?? 0} open positions
            </span>
            {ctStats?.paused && (
              <span className="ml-1 text-amber-400 text-xs font-bold animate-pulse">⏸ PAUSED</span>
            )}
          </div>
        ) : (
          <div className="flex-1 bg-amber-500/10 border border-amber-500/30 rounded-lg px-4 py-2 flex items-center gap-3">
            <div className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
            <span className="text-amber-400 text-sm font-semibold tracking-wide">
              PAPER TRADING MODE — No real funds at risk
            </span>
            {ctStats?.paused && (
              <span className="ml-1 text-amber-400 text-xs font-bold animate-pulse">⏸ PAUSED</span>
            )}
          </div>
        )}

        <button
          onClick={togglePause}
          disabled={pauseLoading}
          className="flex-shrink-0 flex items-center gap-2 px-5 py-2 rounded-lg font-bold text-sm tracking-wide transition-all duration-150 disabled:opacity-50"
          style={ctStats?.paused ? {
            background: 'rgba(52,211,153,0.15)',
            border: '1px solid rgba(52,211,153,0.5)',
            color: '#6ee7b7',
          } : {
            background: 'rgba(239,68,68,0.15)',
            border: '1px solid rgba(239,68,68,0.5)',
            color: '#f87171',
          }}
        >
          {pauseLoading
            ? <span className="animate-spin text-base">⟳</span>
            : ctStats?.paused ? <>▶ RESUME</> : <>⏸ PAUSE</>}
        </button>
      </div>}

      {/* ── Row 1: Summary bar ────────────────────────────────────────────── */}
      {ctStats && (
        <CopyTradeSummaryBar
          balance={ctStats.balance}
          netPnl={ctStats.net_pnl}
          trades={ctStats.trades}
          wins={ctStats.wins}
          openCount={ctStats.open_count}
          maxSlots={Number(config?.monster_max_concurrent ?? 1)}
          signalsToday={ctStats.signals_today ?? 0}
          watchedWallets={ctStats.watched_wallets}
          tradeSize={Number(config?.monster_default_size_sol ?? 0.45)}
          liveMode={ctStats.live_mode}
        />
      )}

      <ZoneHeader label="Command" hint={copyTradeEnabled ? "Live positions · Jarvis voice interface · click a position to focus brains" : "Jarvis voice interface · brain decisions"} />

      {/* ── Row 2: Copy Trade Positions (copy-trade only) + Jarvis chat ── */}
      {copyTradeEnabled && (
        <div className="col-span-12 lg:col-span-7">
          <CopyTradePositions selectedMint={selectedMint} onSelectMint={setSelectedMint} />
        </div>
      )}
      <div className={`col-span-12 ${copyTradeEnabled ? 'lg:col-span-5' : ''} h-[460px]`}>
        <BotChat sendCommand={sendCommand} />
      </div>

      <ZoneHeader label="Intelligence" hint="Groq 30s → Gemini 2m → Claude 5m · learning from every trade" />

      {/* ── Row 2c: AI Brain panel — Groq/Gemini/Claude live decisions ─── */}
      <div className="col-span-12">
        <BrainPanel mint={selectedMint ?? positions[0]?.mint} />
      </div>

      <ZoneHeader label="Scanner" hint="PumpSwap & Meteora · AI-monitored · manual close available" />

      {/* ── Row 2b: Scanner Positions (PumpSwap & Meteora) ───────────────── */}
      <div className="col-span-12">
        <PositionsTable />
      </div>

      {/* ── Row 3: Live charts (copy-trade only) ───────────────────────── */}
      {copyTradeEnabled && (
        <div className="col-span-12">
          <div className="flex items-center gap-2 mb-3 px-1">
            <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">Live Charts</span>
            {positions.length > 0 && (
              <span className="text-zinc-600 text-xs normal-case">
                — {positions.length} open position{positions.length !== 1 ? 's' : ''} · auto-updates every 2s
              </span>
            )}
          </div>
          <CopyTradeLiveCharts positions={positions} />
        </div>
      )}

      <ZoneHeader label="Performance" hint={copyTradeEnabled ? "Wallet promotion · Trade history · Activity feed" : "Monster history · Activity feed"} />

      {/* ── Row 3b: Wallet promotion board (copy-trade only) ─────────────── */}
      {copyTradeEnabled && (
        <div className="col-span-12">
          <WalletPromotionPanel />
        </div>
      )}

      {/* ── Row 4: Trade history + Activity feed ─────────────────────────── */}
      <div className="col-span-12 lg:col-span-8 h-[420px]">
        <CopyTradeHistoryTable
          trades={trades}
          netPnl={ctStats?.net_pnl ?? 0}
          winRate={ctStats?.win_rate ?? 0}
          wins={ctStats?.wins ?? 0}
          total={ctStats?.trades ?? 0}
        />
      </div>
      <div className="col-span-12 lg:col-span-4 h-[420px] overflow-hidden">
        <ActivityFeed />
      </div>

      <ZoneHeader label="Creator-Alpha" hint="ONLY active strategy · 0.1 SOL × 3 slots · bonding-curve direct entry · brain-driven exits" />

      {/* ── Creator-alpha P&L hero panel — strategy-specific stats ──────── */}
      <div className="col-span-12 h-[400px]">
        <CreatorAlphaPnLPanel />
      </div>

      {/* ── Phantom Terminal — full width, replaces operator feed ─────────── */}
      <div className="col-span-12 h-[480px]">
        <PhantomTerminalPanel />
      </div>

      <ZoneHeader label="Controls" hint="Live position-sizing · no restart required" />

      {/* ── Position Config — standalone ────────────────────────────────── */}
      <div className="col-span-12 lg:col-span-5">
        <PositionConfigPanel
          solBalance={wallet?.sol_balance ?? 0}
          currentMaxConcurrent={Number(config?.monster_max_concurrent ?? 1)}
          currentTradeSize={Number(config?.monster_default_size_sol ?? 0.45)}
          onApplied={() => fetchStatus().then(setInitialState).catch(() => {})}
        />
      </div>

    </DashboardLayout>
  )
}

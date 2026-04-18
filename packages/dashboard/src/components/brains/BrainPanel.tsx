import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'

type BrainName = 'groq' | 'gemini' | 'claude'

interface Decision {
  ts: string
  mint: string
  token: string
  action: string
  reason: string
  pnl_pct_at_decision: number | null
  confidence: number | null
  outcome: string
  final_pnl_pct: number | null
}

interface SessionStats {
  total_calls?: number
  holds_recommended?: number
  exits_recommended?: number
  correct_hold_then_tp?: number
  premature_exit_missed_tp?: number
  correct_exit_before_sl?: number
  missed_rug_held_too_long?: number
}

interface HistoricalSummary {
  total_trades?: number
  win_rate_pct?: number
  net_sol?: number
  by_wallet?: Record<string, { wr: number; net_sol: number; trades: number }>
}

interface BrainData {
  last_updated?: string
  session_stats: SessionStats
  learned_patterns: string[]
  historical_summary: HistoricalSummary
  recent_decisions: Decision[]
  decision_count: number
}

interface BrainPayload {
  brains: Record<BrainName, BrainData>
  mint_filter: string | null
}

const BRAIN_META: Record<BrainName, { label: string; role: string; tick: string; color: string; accent: string }> = {
  groq:   { label: 'GROQ',   role: 'Speed · llama-3.3-70b',   tick: '30s',  color: '#fb923c', accent: 'rgba(249,115,22,0.15)' },
  gemini: { label: 'GEMINI', role: 'Analysis · gemini-2.0',   tick: '2m',   color: '#60a5fa', accent: 'rgba(59,130,246,0.15)' },
  claude: { label: 'CLAUDE', role: 'Depth · sonnet',          tick: '5m',   color: '#a78bfa', accent: 'rgba(139,92,246,0.15)' },
}

const ACTION_STYLES: Record<string, { bg: string; color: string; icon: string }> = {
  RUNNER:    { bg: 'rgba(52,211,153,0.18)', color: '#34d399', icon: '🚀' },
  HOLD:      { bg: 'rgba(148,163,184,0.15)', color: '#cbd5e1', icon: '⏸' },
  WATCH:     { bg: 'rgba(249,115,22,0.15)', color: '#fb923c', icon: '👁' },
  SELL:      { bg: 'rgba(239,68,68,0.2)',   color: '#f87171', icon: '⬇' },
  EXIT:      { bg: 'rgba(239,68,68,0.2)',   color: '#f87171', icon: '⬇' },
  RUG_RISK:  { bg: 'rgba(239,68,68,0.25)',  color: '#fca5a5', icon: '⚠' },
  ENTRY_OK:  { bg: 'rgba(52,211,153,0.15)', color: '#34d399', icon: '✓' },
  ENTRY_WARN:{ bg: 'rgba(251,191,36,0.15)', color: '#fbbf24', icon: '⚠' },
  NORMAL:    { bg: 'rgba(148,163,184,0.12)', color: '#94a3b8', icon: '◐' },
}

function actionStyle(action: string) {
  return ACTION_STYLES[action] ?? { bg: 'rgba(148,163,184,0.1)', color: '#94a3b8', icon: '·' }
}

function formatAge(iso?: string): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  const secs = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (secs < 60)   return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  return `${Math.floor(secs / 3600)}h ago`
}

function BrainColumn({ name, data, hasFilter }: { name: BrainName; data: BrainData; hasFilter: boolean }) {
  const meta = BRAIN_META[name]
  const decisions = data.recent_decisions
  const lastTs = decisions[0]?.ts ?? data.last_updated
  const stats = data.session_stats
  const totalCalls = stats.total_calls ?? 0
  const holds = stats.holds_recommended ?? 0
  const exits = stats.exits_recommended ?? 0
  // "Awake" = last decision within one tick cycle × 3 (generous)
  const awakeThresholdSecs = name === 'groq' ? 90 : name === 'gemini' ? 360 : 900
  const ageSecs = lastTs ? Math.max(0, Math.floor((Date.now() - new Date(lastTs).getTime()) / 1000)) : 99999
  const isAwake = ageSecs < awakeThresholdSecs

  return (
    <div className="flex flex-col gap-2 min-w-0">
      {/* Brain header */}
      <div className="flex items-center gap-2 px-2 py-1.5 rounded border"
        style={{
          background: meta.accent,
          borderColor: meta.color + '40',
        }}>
        <div className="w-2 h-2 rounded-full" style={{
          background: isAwake ? meta.color : 'rgba(100,116,139,0.5)',
          boxShadow: isAwake ? `0 0 8px ${meta.color}` : 'none',
          animation: isAwake ? 'pulse 1.4s ease-in-out infinite' : 'none',
        }} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] font-bold tracking-wider" style={{ color: meta.color }}>
              {meta.label}
            </span>
            <span className="text-[9px] px-1 py-0.5 rounded font-mono"
              style={{ background: 'rgba(0,0,0,0.3)', color: meta.color }}>
              {meta.tick}
            </span>
            {isAwake ? (
              <span className="text-[9px] font-bold text-emerald-400">● AWAKE</span>
            ) : (
              <span className="text-[9px] text-zinc-600">○ idle</span>
            )}
          </div>
          <div className="text-[9px] text-zinc-500 truncate">{meta.role}</div>
        </div>
      </div>

      {/* Session stats */}
      <div className="flex items-center gap-2 text-[9px] text-zinc-500 px-2">
        <span>calls: <span className="text-zinc-300 font-mono">{totalCalls}</span></span>
        <span>·</span>
        <span className="text-emerald-500">hold {holds}</span>
        <span>·</span>
        <span className="text-red-400">exit {exits}</span>
      </div>

      {/* Recent decisions */}
      <div className="flex-1 overflow-y-auto max-h-[180px] pr-1 space-y-1">
        {decisions.length === 0 ? (
          <div className="text-[10px] text-zinc-600 italic px-2 py-3 text-center">
            {hasFilter
              ? 'No decisions recorded yet for this position.'
              : 'No recent decisions — brain awaits next tick.'}
          </div>
        ) : (
          decisions.slice(0, 8).map((d, i) => {
            const act = actionStyle(d.action)
            return (
              <div key={i} className="px-2 py-1.5 rounded border border-zinc-800/50 bg-zinc-900/30">
                <div className="flex items-center justify-between gap-2 mb-0.5">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="text-[9px] px-1 py-0.5 rounded font-bold flex items-center gap-0.5"
                      style={{ background: act.bg, color: act.color }}>
                      <span>{act.icon}</span>
                      <span>{d.action}</span>
                    </span>
                    {d.confidence != null && (
                      <span className="text-[9px] text-zinc-500 font-mono">
                        {(d.confidence * 100).toFixed(0)}%
                      </span>
                    )}
                  </div>
                  <span className="text-[9px] text-zinc-600 flex-shrink-0">{formatAge(d.ts)}</span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] text-zinc-300 font-mono truncate">{d.token}</span>
                  {d.pnl_pct_at_decision != null && (
                    <span className={`text-[9px] font-mono ${d.pnl_pct_at_decision >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {d.pnl_pct_at_decision >= 0 ? '+' : ''}{d.pnl_pct_at_decision.toFixed(1)}%
                    </span>
                  )}
                </div>
                {d.reason && (
                  <div className="text-[9px] text-zinc-500 italic mt-0.5 line-clamp-2">
                    {d.reason}
                  </div>
                )}
              </div>
            )
          })
        )}
      </div>

      {/* Learned patterns (collapsed) */}
      {data.learned_patterns.length > 0 && (
        <details className="px-2">
          <summary className="text-[9px] text-zinc-500 cursor-pointer hover:text-zinc-300">
            📚 {data.learned_patterns.length} learned pattern{data.learned_patterns.length !== 1 ? 's' : ''}
          </summary>
          <ul className="mt-1 space-y-0.5 text-[9px] text-zinc-400">
            {data.learned_patterns.slice(0, 5).map((p, i) => (
              <li key={i} className="pl-2 border-l border-zinc-700/50">{p}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

export default function BrainPanel({ mint }: { mint?: string }) {
  const [payload, setPayload] = useState<BrainPayload | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const load = async () => {
    setRefreshing(true)
    try {
      const url = mint ? `/api/brain-memory?mint=${mint}` : '/api/brain-memory'
      const res = await fetch(url)
      if (res.ok) setPayload(await res.json())
    } catch { /* ignore */ }
    finally { setRefreshing(false) }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 5_000) // 5s refresh — fast enough to see brains waking
    return () => clearInterval(id)

  }, [mint])

  const brains = payload?.brains
  const hasFilter = !!payload?.mint_filter

  return (
    <GlassCard>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">
            AI Quant Brains
          </span>
          {hasFilter && (
            <span className="text-[10px] px-2 py-0.5 rounded bg-orange-500/15 text-orange-400 font-mono">
              filter: {payload?.mint_filter?.slice(0, 8)}…
            </span>
          )}
          {refreshing && <span className="text-[10px] text-zinc-500 animate-pulse">polling…</span>}
        </div>
        <div className="text-[10px] text-zinc-500">
          Groq 30s → Gemini 2m → Claude 5m cascade · auto-refresh 5s
        </div>
      </div>

      {!brains ? (
        <div className="text-center py-8 text-zinc-600 text-sm">Loading brain memory…</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {(['groq', 'gemini', 'claude'] as const).map(name => (
            <BrainColumn key={name} name={name} data={brains[name]} hasFilter={hasFilter} />
          ))}
        </div>
      )}
    </GlassCard>
  )
}

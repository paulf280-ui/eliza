import { useEffect, useState, useCallback } from 'react'
import { GlassCard } from '../common/GlassCard'

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
}

interface TierHealth {
  status?: 'ok' | 'idle' | 'down'
  last_err_msg?: string
}

interface BrainData {
  last_updated?: string
  session_stats: SessionStats
  learned_patterns: string[]
  recent_decisions: Decision[]
  tier_health?: TierHealth
}

interface BrainPayload {
  brains: { groq: BrainData; gemini: BrainData; claude: BrainData }
  mint_filter: string | null
}

const ACTION_STYLES: Record<string, { bg: string; color: string; icon: string }> = {
  RUNNER:    { bg: 'rgba(52,211,153,0.18)',  color: '#34d399', icon: '🚀' },
  HOLD:      { bg: 'rgba(148,163,184,0.15)', color: '#cbd5e1', icon: '⏸' },
  WATCH:     { bg: 'rgba(249,115,22,0.15)',  color: '#fb923c', icon: '👁' },
  SELL:      { bg: 'rgba(239,68,68,0.2)',    color: '#f87171', icon: '⬇' },
  EXIT:      { bg: 'rgba(239,68,68,0.2)',    color: '#f87171', icon: '⬇' },
  RUG_RISK:  { bg: 'rgba(239,68,68,0.25)',   color: '#fca5a5', icon: '⚠' },
  ENTRY_OK:  { bg: 'rgba(52,211,153,0.15)',  color: '#34d399', icon: '✓' },
  ENTRY_WARN:{ bg: 'rgba(251,191,36,0.15)',  color: '#fbbf24', icon: '⚠' },
  NORMAL:    { bg: 'rgba(148,163,184,0.12)', color: '#94a3b8', icon: '◐' },
}

function actionStyle(action: string) {
  return ACTION_STYLES[action] ?? { bg: 'rgba(148,163,184,0.1)', color: '#94a3b8', icon: '·' }
}

function formatAge(iso?: string): string {
  if (!iso) return '—'
  const secs = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
  if (secs < 60)   return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  return `${Math.floor(secs / 3600)}h ago`
}

export default function BrainPanel({ mint }: { mint?: string }) {
  const [payload, setPayload] = useState<BrainPayload | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [waking, setWaking] = useState(false)
  const [wakeMsg, setWakeMsg] = useState<string | null>(null)

  const load = useCallback(async () => {
    setRefreshing(true)
    try {
      const url = mint ? `/api/brain-memory?mint=${mint}` : '/api/brain-memory'
      const res = await fetch(url)
      if (res.ok) setPayload(await res.json())
    } catch { /* ignore */ }
    finally { setRefreshing(false) }
  }, [mint])

  useEffect(() => {
    load()
    const id = setInterval(load, 5_000)
    return () => clearInterval(id)
  }, [load])

  async function wakeGroq() {
    if (waking) return
    setWaking(true)
    setWakeMsg(null)
    try {
      const res = await fetch('/api/brain/wake-groq', { method: 'POST' })
      const data = await res.json()
      if (data.ok) {
        setWakeMsg(data.count === 0 ? 'no active positions' : `waking ${data.count} position${data.count !== 1 ? 's' : ''}…`)
        setTimeout(load, 2000) // pull fresh decisions after Groq fires
      } else {
        setWakeMsg(`error: ${data.error}`)
      }
    } catch {
      setWakeMsg('request failed')
    } finally {
      setWaking(false)
      setTimeout(() => setWakeMsg(null), 5000)
    }
  }

  const groq = payload?.brains?.groq
  const hasFilter = !!payload?.mint_filter
  const decisions = groq?.recent_decisions ?? []
  const stats = groq?.session_stats ?? {}
  const health = groq?.tier_health

  const lastTs = decisions[0]?.ts ?? groq?.last_updated
  const ageSecs = lastTs ? Math.max(0, Math.floor((Date.now() - new Date(lastTs).getTime()) / 1000)) : 99999
  const isAwake = ageSecs < 90
  const isDown = health?.status === 'down'

  return (
    <GlassCard>
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">AI Quant Brain</span>
          {hasFilter && (
            <span className="text-[10px] px-2 py-0.5 rounded bg-orange-500/15 text-orange-400 font-mono">
              filter: {payload?.mint_filter?.slice(0, 8)}…
            </span>
          )}
          {refreshing && <span className="text-[10px] text-zinc-500 animate-pulse">polling…</span>}
        </div>

        <div className="flex items-center gap-3">
          {wakeMsg && (
            <span className={`text-[10px] font-mono ${wakeMsg.startsWith('error') ? 'text-red-400' : 'text-emerald-400'}`}>
              {wakeMsg}
            </span>
          )}
          <button
            onClick={wakeGroq}
            disabled={waking}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md font-semibold transition-all disabled:opacity-50"
            style={{ background: 'rgba(249,115,22,0.15)', border: '1px solid rgba(249,115,22,0.4)', color: '#fb923c' }}
            title="Force Groq to run immediately on all active positions"
          >
            {waking ? (
              <span className="animate-spin text-sm">⟳</span>
            ) : (
              <span>⚡</span>
            )}
            Wake Groq
          </button>
        </div>
      </div>

      {!groq ? (
        <div className="text-center py-8 text-zinc-600 text-sm">Loading brain memory…</div>
      ) : (
        <div className="flex gap-6">
          {/* Left: Groq header + stats + health */}
          <div className="flex-shrink-0 w-56">
            {/* Status badge */}
            <div className="flex items-center gap-2 px-3 py-2 rounded border mb-3"
              style={{ background: 'rgba(249,115,22,0.12)', borderColor: 'rgba(249,115,22,0.3)' }}>
              <div className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{
                background: isAwake ? '#fb923c' : 'rgba(100,116,139,0.5)',
                boxShadow: isAwake ? '0 0 8px #fb923c' : 'none',
                animation: isAwake ? 'pulse 1.4s ease-in-out infinite' : 'none',
              }} />
              <div>
                <div className="flex items-center gap-1.5">
                  <span className="text-[11px] font-bold tracking-wider text-orange-400">GROQ</span>
                  <span className="text-[9px] px-1 py-0.5 rounded font-mono bg-black/30 text-orange-400">30s</span>
                  {isDown ? (
                    <span className="text-[9px] font-bold text-red-400">● DOWN</span>
                  ) : isAwake ? (
                    <span className="text-[9px] font-bold text-emerald-400">● AWAKE</span>
                  ) : (
                    <span className="text-[9px] text-zinc-600">○ idle</span>
                  )}
                </div>
                <div className="text-[9px] text-zinc-500">Speed · llama-3.3-70b</div>
              </div>
            </div>

            {isDown && health?.last_err_msg && (
              <div className="text-[9px] text-red-400 px-2 py-1 rounded border border-red-500/30 bg-red-500/5 mb-3 line-clamp-2">
                ⚠ {health.last_err_msg}
              </div>
            )}

            {/* Session stats */}
            <div className="space-y-1.5 text-xs text-zinc-500 px-1">
              <div className="flex justify-between">
                <span>Total calls</span>
                <span className="font-mono text-zinc-300">{stats.total_calls ?? 0}</span>
              </div>
              <div className="flex justify-between">
                <span>Hold</span>
                <span className="font-mono text-emerald-400">{stats.holds_recommended ?? 0}</span>
              </div>
              <div className="flex justify-between">
                <span>Exit</span>
                <span className="font-mono text-red-400">{stats.exits_recommended ?? 0}</span>
              </div>
              <div className="flex justify-between">
                <span>Last fired</span>
                <span className="font-mono text-zinc-400">{formatAge(lastTs)}</span>
              </div>
            </div>

            {/* Learned patterns */}
            {groq.learned_patterns.length > 0 && (
              <details className="mt-3 px-1">
                <summary className="text-[9px] text-zinc-500 cursor-pointer hover:text-zinc-300">
                  📚 {groq.learned_patterns.length} learned pattern{groq.learned_patterns.length !== 1 ? 's' : ''}
                </summary>
                <ul className="mt-1 space-y-0.5 text-[9px] text-zinc-400">
                  {groq.learned_patterns.slice(0, 5).map((p, i) => (
                    <li key={i} className="pl-2 border-l border-zinc-700/50">{p}</li>
                  ))}
                </ul>
              </details>
            )}
          </div>

          {/* Right: decisions feed — full remaining width */}
          <div className="flex-1 min-w-0">
            <div className="text-[9px] text-zinc-600 uppercase tracking-widest mb-2 px-1">Recent decisions</div>
            {decisions.length === 0 ? (
              <div className="text-[10px] text-zinc-600 italic px-2 py-4 text-center">
                {hasFilter ? 'No decisions recorded yet for this position.' : 'No recent decisions — Groq awaits next tick.'}
              </div>
            ) : (
              <div className="grid grid-cols-2 xl:grid-cols-3 gap-2">
                {decisions.slice(0, 12).map((d, i) => {
                  const act = actionStyle(d.action)
                  return (
                    <div key={i} className="px-2 py-2 rounded border border-zinc-800/50 bg-zinc-900/30">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span className="text-[9px] px-1.5 py-0.5 rounded font-bold flex items-center gap-0.5 flex-shrink-0"
                            style={{ background: act.bg, color: act.color }}>
                            <span>{act.icon}</span>
                            <span>{d.action}</span>
                          </span>
                          {d.confidence != null && (
                            <span className="text-[9px] text-zinc-500 font-mono flex-shrink-0">
                              {(d.confidence * 100).toFixed(0)}%
                            </span>
                          )}
                        </div>
                        <span className="text-[9px] text-zinc-600 flex-shrink-0">{formatAge(d.ts)}</span>
                      </div>
                      <div className="flex items-center justify-between gap-2 mb-0.5">
                        <span className="text-[10px] text-zinc-300 font-mono truncate">{d.token}</span>
                        {d.pnl_pct_at_decision != null && (
                          <span className={`text-[9px] font-mono flex-shrink-0 ${d.pnl_pct_at_decision >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {d.pnl_pct_at_decision >= 0 ? '+' : ''}{d.pnl_pct_at_decision.toFixed(1)}%
                          </span>
                        )}
                      </div>
                      {d.reason && (
                        <div className="text-[9px] text-zinc-500 italic line-clamp-2">{d.reason}</div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </GlassCard>
  )
}

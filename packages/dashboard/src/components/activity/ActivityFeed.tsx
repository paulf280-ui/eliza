import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'

interface Signal {
  ts: number
  mint?: string
  token_name?: string
  wallet?: string
  sol?: number
  action: string          // "entered" | "skipped" | "sold_by_whale" | "closed" | "alert"
  skip_reason?: string    // "previously_traded" | "slots_full" | "duplicate" | "insufficient_balance"
  pnl_pct?: number
  reason?: string         // close reason from closed trades
}

function relTime(ts: number): string {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60)   return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

function SignalRow({ sig }: { sig: Signal }) {
  const name = sig.token_name || sig.mint?.slice(0, 8) || '?'
  const wallet = sig.wallet || ''

  let icon = '📡'
  let text = ''
  let color = 'text-zinc-400'
  let dotColor = 'bg-zinc-600'

  if (sig.action === 'entered') {
    icon = '🟢'
    text = `ENTERED ${name} — following ${wallet} @ ${sig.sol?.toFixed(2)} SOL`
    color = 'text-emerald-300'
    dotColor = 'bg-emerald-500'
  } else if (sig.action === 'skipped') {
    const reason = sig.skip_reason || 'unknown'
    if (reason === 'previously_traded') {
      icon = '🚫'
      text = `BLOCKED ${name} — golden rule (already traded)`
      color = 'text-zinc-500'
      dotColor = 'bg-zinc-700'
    } else if (reason === 'slots_full') {
      icon = '⏳'
      text = `SKIPPED ${name} by ${wallet} — slots full`
      color = 'text-amber-600'
      dotColor = 'bg-amber-700'
    } else if (reason === 'duplicate') {
      icon = '↩️'
      text = `DUPLICATE ${name} — already in position`
      color = 'text-zinc-600'
      dotColor = 'bg-zinc-700'
    } else if (reason === 'insufficient_balance') {
      icon = '💸'
      text = `SKIPPED ${name} — insufficient balance`
      color = 'text-red-500'
      dotColor = 'bg-red-700'
    } else if (reason === 'not_indexed') {
      icon = '🚫'
      text = `BLOCKED ${name} — not on DexScreener (bonding curve / unverifiable)`
      color = 'text-red-500'
      dotColor = 'bg-red-700'
    } else if (reason?.startsWith('mc_too_low')) {
      const mc = reason.replace('mc_too_low_', '')
      icon = '🚫'
      text = `BLOCKED ${name} — MC ${mc} below $7.5k floor`
      color = 'text-red-400'
      dotColor = 'bg-red-600'
    } else if (reason?.startsWith('liq_too_low')) {
      const liq = reason.replace('liq_too_low_', '')
      icon = '🚫'
      text = `BLOCKED ${name} — liquidity ${liq} below $12.5k floor`
      color = 'text-red-400'
      dotColor = 'bg-red-600'
    } else {
      icon = '⚪'
      text = `SKIPPED ${name} by ${wallet} — ${reason}`
      color = 'text-zinc-500'
      dotColor = 'bg-zinc-700'
    }
  } else if (sig.action === 'sold_by_whale') {
    icon = '📤'
    text = `${wallet} SOLD ${name} — closing position`
    color = 'text-blue-300'
    dotColor = 'bg-blue-500'
  } else if (sig.action === 'closed' || sig.reason) {
    const r = sig.reason || sig.action || ''
    const pnl = sig.pnl_pct
    if (r.startsWith('stop_loss')) {
      icon = '🛑'
      text = `SL ${name}${pnl != null ? ` ${pnl > 0 ? '+' : ''}${pnl.toFixed(1)}%` : ''}`
      color = 'text-red-400'
      dotColor = 'bg-red-500'
    } else if (r.startsWith('stagnant')) {
      icon = '💤'
      text = `STAGNANT EXIT ${name} — low MC, no movement${pnl != null ? ` (${pnl > 0 ? '+' : ''}${pnl.toFixed(1)}%)` : ''}`
      color = 'text-amber-400'
      dotColor = 'bg-amber-500'
    } else if (r.startsWith('wallet_exit')) {
      icon = '📤'
      const w = r.replace('wallet_exit:', '')
      text = `WHALE EXIT ${name} — ${w} sold${pnl != null ? ` | P&L: ${pnl > 0 ? '+' : ''}${pnl.toFixed(1)}%` : ''}`
      color = pnl != null && pnl > 0 ? 'text-emerald-400' : 'text-zinc-400'
      dotColor = pnl != null && pnl > 0 ? 'bg-emerald-500' : 'bg-zinc-500'
    } else if (r.startsWith('take_profit')) {
      const pct = r.replace('take_profit_', '').replace('pct', '')
      icon = '💰'
      text = `TAKE PROFIT ${name} +${pct}%${pnl != null ? ` — locked in ${pnl > 0 ? '+' : ''}${pnl.toFixed(1)}%` : ''}`
      color = 'text-emerald-300'
      dotColor = 'bg-emerald-400'
    } else if (r === 'manual_close') {
      icon = '✋'
      text = `MANUAL CLOSE ${name}${pnl != null ? ` | P&L: ${pnl > 0 ? '+' : ''}${pnl.toFixed(1)}%` : ''}`
      color = pnl != null && pnl > 0 ? 'text-emerald-400' : 'text-amber-400'
      dotColor = 'bg-amber-500'
    } else {
      icon = '📋'
      text = `CLOSED ${name}${pnl != null ? ` ${pnl > 0 ? '+' : ''}${pnl.toFixed(1)}%` : ''} (${r})`
      color = pnl != null && pnl > 0 ? 'text-emerald-400' : 'text-red-400'
      dotColor = pnl != null && pnl > 0 ? 'bg-emerald-500' : 'bg-red-500'
    }
  }

  return (
    <div className="flex items-start gap-2 py-1.5 border-b border-zinc-800/40 group">
      <div className={`w-1.5 h-1.5 rounded-full mt-1.5 flex-shrink-0 ${dotColor}`} />
      <div className="flex-1 min-w-0">
        <span className={`text-[11px] leading-relaxed ${color} break-words`}>
          <span className="mr-1">{icon}</span>
          {text}
        </span>
      </div>
      <span className="text-[10px] text-zinc-700 flex-shrink-0 font-mono tabular-nums">
        {relTime(sig.ts)}
      </span>
    </div>
  )
}

export default function ActivityFeed() {
  const [signals, setSignals] = useState<Signal[]>([])

  useEffect(() => {
    const fetchSignals = async () => {
      try {
        const res = await fetch('/api/copy-trade/stats')
        if (!res.ok) return
        const data = await res.json()

        // Merge recent_signals + closed trades into one feed, sorted newest first
        const sigFeed: Signal[] = [...(data.recent_signals || [])]

        // Inject closed trade events if not already in signal log
        const sigMints = new Set(sigFeed.map((s: Signal) => `${s.mint}${s.action}`))
        for (const t of (data.recent_trades || []).slice(0, 20)) {
          const key = `${t.mint}closed`
          if (!sigMints.has(key)) {
            sigFeed.push({
              ts: t.ts || 0,
              mint: t.mint,
              token_name: t.token_name,
              wallet: t.wallet,
              sol: t.sol_spent,
              action: 'closed',
              reason: t.reason,
              pnl_pct: t.pnl_pct,
            })
          }
        }

        // Sort newest first, cap at 60
        sigFeed.sort((a, b) => (b.ts || 0) - (a.ts || 0))
        setSignals(sigFeed.slice(0, 60))
      } catch { /* ignore */ }
    }

    fetchSignals()
    const id = setInterval(fetchSignals, 3000)
    return () => clearInterval(id)
  }, [])

  return (
    <GlassCard className="flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between mb-3 flex-shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">
            Signal Feed
          </span>
          {signals.length > 0 && (
            <span className="text-[10px] text-zinc-600">{signals.length} events</span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
          <span className="text-[9px] text-zinc-600 uppercase tracking-widest">live</span>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto pr-1 custom-scrollbar">
        {signals.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-zinc-700">
            <div className="text-2xl">📡</div>
            <div className="text-xs">Watching 6 wallets — signals appear here</div>
          </div>
        ) : (
          signals.map((sig, i) => <SignalRow key={i} sig={sig} />)
        )}
      </div>
    </GlassCard>
  )
}

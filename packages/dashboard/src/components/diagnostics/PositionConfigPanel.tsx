import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'
import { patchConfig } from '../../api/client'

interface Props {
  solBalance: number
  currentMaxConcurrent: number
  currentTradeSize: number
  onApplied?: () => void
}

const SLIPPAGE_BUFFER = 1.5  // assume 50% slippage headroom needed per trade

export default function PositionConfigPanel({
  solBalance,
  currentMaxConcurrent,
  currentTradeSize,
  onApplied,
}: Props) {
  const [maxConc, setMaxConc] = useState(currentMaxConcurrent)
  const [tradeSize, setTradeSize] = useState(currentTradeSize)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  // Keep local state in sync if backend updates push new values (e.g. via WS).
  useEffect(() => { setMaxConc(currentMaxConcurrent) }, [currentMaxConcurrent])
  useEffect(() => { setTradeSize(currentTradeSize) }, [currentTradeSize])

  const requiredSol = maxConc * tradeSize * SLIPPAGE_BUFFER
  const overBudget = requiredSol > solBalance
  const utilizationPct = solBalance > 0 ? (requiredSol / solBalance) * 100 : 0
  const dirty = maxConc !== currentMaxConcurrent || tradeSize !== currentTradeSize

  async function apply() {
    setBusy(true)
    setMsg(null)
    try {
      const result = await patchConfig({
        monster_max_concurrent: maxConc,
        monster_default_size_sol: tradeSize,
        creator_alpha_size_sol: tradeSize,        // keep in sync — same strategy, same size
        creator_alpha_max_concurrent: maxConc,    // keep in sync — same concurrent cap
      })
      const failed = Object.entries(result).filter(
        ([, v]) => !((v as { ok: boolean }).ok)
      )
      if (failed.length) {
        setMsg(`partial: ${failed.map(([k, v]) => `${k}=${(v as { message: string }).message}`).join(', ')}`)
      } else {
        setMsg('applied — bot picks up next cycle')
        onApplied?.()
      }
    } catch (e) {
      setMsg(`error: ${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    setMaxConc(currentMaxConcurrent)
    setTradeSize(currentTradeSize)
    setMsg(null)
  }

  return (
    <GlassCard className="h-full">
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-400 font-semibold">
            Position sizing
          </div>
          <div className="text-zinc-500 text-xs mt-0.5">
            scale up as wallet grows · live config (no restart)
          </div>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">wallet</div>
          <div className="text-zinc-100 font-mono text-sm">{solBalance.toFixed(4)} SOL</div>
        </div>
      </div>

      <div className="space-y-4">
        <div>
          <div className="flex items-baseline justify-between mb-1.5">
            <label className="text-xs text-zinc-300 font-mono">max concurrent positions</label>
            <span className="text-xs text-zinc-100 font-mono">{maxConc}</span>
          </div>
          <input
            type="range"
            min={1}
            max={5}
            step={1}
            value={maxConc}
            onChange={e => setMaxConc(parseInt(e.target.value, 10))}
            disabled={busy}
            className="w-full"
          />
          <div className="flex justify-between text-[10px] text-zinc-600 font-mono mt-0.5">
            <span>1</span><span>2</span><span>3</span><span>4</span><span>5</span>
          </div>
        </div>

        <div>
          <div className="flex items-baseline justify-between mb-1.5">
            <label className="text-xs text-zinc-300 font-mono">trade size (SOL per position)</label>
            <span className="text-xs text-zinc-100 font-mono">{tradeSize.toFixed(2)} SOL</span>
          </div>
          <input
            type="range"
            min={0.05}
            max={2.0}
            step={0.05}
            value={tradeSize}
            onChange={e => setTradeSize(parseFloat(e.target.value))}
            disabled={busy}
            className="w-full"
          />
          <div className="flex justify-between text-[10px] text-zinc-600 font-mono mt-0.5">
            <span>0.05</span><span>0.5</span><span>1.0</span><span>1.5</span><span>2.0</span>
          </div>
        </div>

        <div className="bg-zinc-800/40 rounded-md px-3 py-2 text-xs space-y-1">
          <div className="flex justify-between">
            <span className="text-zinc-400">required SOL (×{SLIPPAGE_BUFFER} slippage buffer):</span>
            <span className={`font-mono ${overBudget ? 'text-red-400' : 'text-emerald-400'}`}>
              {requiredSol.toFixed(3)} SOL
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-zinc-400">wallet utilization:</span>
            <span className={`font-mono ${utilizationPct > 80 ? 'text-amber-400' : 'text-zinc-300'}`}>
              {utilizationPct.toFixed(0)}%
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={apply}
            disabled={busy || !dirty || overBudget}
            className="flex-1 py-2 rounded-md font-bold text-sm transition-all disabled:opacity-40"
            style={{
              background: dirty && !overBudget ? 'rgba(52,211,153,0.18)' : 'rgba(82,82,91,0.3)',
              border: `1px solid ${dirty && !overBudget ? 'rgba(52,211,153,0.5)' : 'rgba(82,82,91,0.5)'}`,
              color: dirty && !overBudget ? '#6ee7b7' : '#a1a1aa',
            }}
          >
            {busy ? 'applying…' : overBudget ? 'over budget — adjust' : dirty ? 'apply' : 'no changes'}
          </button>
          <button
            onClick={reset}
            disabled={busy || !dirty}
            className="px-3 py-2 rounded-md text-xs text-zinc-400 disabled:opacity-40"
            style={{ background: 'rgba(82,82,91,0.2)' }}
          >
            reset
          </button>
        </div>

        {msg && (
          <div className={`text-xs ${msg.startsWith('error') || msg.startsWith('partial') ? 'text-red-400' : 'text-emerald-400'}`}>
            {msg}
          </div>
        )}
      </div>
    </GlassCard>
  )
}

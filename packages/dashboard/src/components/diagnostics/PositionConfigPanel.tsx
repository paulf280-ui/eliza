import { useEffect, useState } from 'react'
import { GlassCard } from '../common/GlassCard'
import { patchConfig } from '../../api/client'

interface Props {
  solBalance: number
  currentMaxConcurrent: number
  currentTradeSize: number
  currentVelocitySize?: number
  onApplied?: () => void
}

const SLIPPAGE_BUFFER = 1.5

function SizeSlider({
  label, value, onChange, disabled, min = 0.05, max = 2.0, step = 0.05,
}: {
  label: string; value: number; onChange: (v: number) => void
  disabled: boolean; min?: number; max?: number; step?: number
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1.5">
        <label className="text-xs text-zinc-300 font-mono">{label}</label>
        <span className="text-xs text-zinc-100 font-mono">{value.toFixed(2)} SOL</span>
      </div>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(parseFloat(e.target.value))}
        disabled={disabled} className="w-full"
      />
      <div className="flex justify-between text-[10px] text-zinc-600 font-mono mt-0.5">
        <span>{min}</span><span>0.5</span><span>1.0</span><span>1.5</span><span>{max}</span>
      </div>
    </div>
  )
}

export default function PositionConfigPanel({
  solBalance,
  currentMaxConcurrent,
  currentTradeSize,
  currentVelocitySize = 0.20,
  onApplied,
}: Props) {
  const [maxConc,      setMaxConc]      = useState(currentMaxConcurrent)
  const [tradeSize,    setTradeSize]    = useState(currentTradeSize)
  const [velSize,      setVelSize]      = useState(currentVelocitySize)
  const [busy,         setBusy]         = useState(false)
  const [msg,          setMsg]          = useState<string | null>(null)

  useEffect(() => { setMaxConc(currentMaxConcurrent) },   [currentMaxConcurrent])
  useEffect(() => { setTradeSize(currentTradeSize) },     [currentTradeSize])
  useEffect(() => { setVelSize(currentVelocitySize) },    [currentVelocitySize])

  // Combined wallet usage: lifecycle (×1.5 slip) + velocity (×1.5 slip)
  const lcRequired  = maxConc * tradeSize * SLIPPAGE_BUFFER
  const velRequired = 1 * velSize * SLIPPAGE_BUFFER          // velocity always 1 slot
  const totalRequired = lcRequired + velRequired
  const overBudget    = totalRequired > solBalance
  const utilizationPct = solBalance > 0 ? (totalRequired / solBalance) * 100 : 0

  const lcDirty  = maxConc !== currentMaxConcurrent || tradeSize !== currentTradeSize
  const velDirty = velSize !== currentVelocitySize
  const dirty    = lcDirty || velDirty

  async function apply() {
    setBusy(true)
    setMsg(null)
    try {
      const payload: Record<string, number> = {}
      if (lcDirty) {
        payload.monster_max_concurrent    = maxConc
        payload.monster_default_size_sol  = tradeSize
        payload.lifecycle_size_sol        = tradeSize
        payload.creator_alpha_size_sol    = tradeSize
        payload.creator_alpha_max_concurrent = maxConc
      }
      if (velDirty) {
        payload.velocity_size_sol = velSize
      }
      const result = await patchConfig(payload)
      const failed = Object.entries(result).filter(([, v]) => !((v as { ok: boolean }).ok))
      if (failed.length) {
        setMsg(`partial: ${failed.map(([k]) => k).join(', ')} failed`)
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
    setVelSize(currentVelocitySize)
    setMsg(null)
  }

  return (
    <GlassCard className="h-full">
      {/* Header */}
      <div className="flex items-baseline justify-between mb-4">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-400 font-semibold">
            Position Sizing
          </div>
          <div className="text-zinc-500 text-xs mt-0.5">live config · no restart required</div>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">wallet</div>
          <div className="text-zinc-100 font-mono text-sm">{solBalance.toFixed(4)} SOL</div>
        </div>
      </div>

      <div className="space-y-5">

        {/* ── LIFECYCLE section ── */}
        <div className="rounded-md border border-teal-500/20 p-3 space-y-3"
             style={{ background: 'rgba(20,184,166,0.04)' }}>
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-sm tracking-wide"
                  style={{ background: 'rgba(20,184,166,0.15)', color: '#2dd4bf', border: '1px solid rgba(20,184,166,0.4)' }}>
              LIFECYCLE
            </span>
            <span className="text-[10px] text-zinc-500">quiet accumulation · +20% TP · -25% floor</span>
          </div>
          <div>
            <div className="flex items-baseline justify-between mb-1.5">
              <label className="text-xs text-zinc-300 font-mono">max concurrent positions</label>
              <span className="text-xs text-zinc-100 font-mono">{maxConc}</span>
            </div>
            <input
              type="range" min={1} max={5} step={1} value={maxConc}
              onChange={e => setMaxConc(parseInt(e.target.value, 10))}
              disabled={busy} className="w-full"
            />
            <div className="flex justify-between text-[10px] text-zinc-600 font-mono mt-0.5">
              <span>1</span><span>2</span><span>3</span><span>4</span><span>5</span>
            </div>
          </div>
          <SizeSlider
            label="trade size (SOL per position)"
            value={tradeSize} onChange={setTradeSize} disabled={busy}
          />
        </div>

        {/* ── VELOCITY section ── */}
        <div className="rounded-md border border-orange-500/25 p-3 space-y-3"
             style={{ background: 'rgba(249,115,22,0.04)' }}>
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-sm tracking-wide"
                  style={{ background: 'rgba(249,115,22,0.2)', color: '#fb923c', border: '1px solid rgba(249,115,22,0.5)' }}>
              ⚡ VELOCITY
            </span>
            <span className="text-[10px] text-zinc-500">1h entry · no SL · +200% TP · 1 slot</span>
          </div>
          <SizeSlider
            label="trade size (SOL per position)"
            value={velSize} onChange={setVelSize} disabled={busy}
          />
          <div className="text-[10px] text-zinc-600 italic">
            Velocity always uses 1 concurrent slot — independent from lifecycle.
          </div>
        </div>

        {/* ── Combined wallet usage ── */}
        <div className="bg-zinc-800/40 rounded-md px-3 py-2 text-xs space-y-1">
          <div className="flex justify-between">
            <span className="text-zinc-400">lifecycle required (×1.5 slippage):</span>
            <span className="font-mono text-teal-400">{lcRequired.toFixed(3)} SOL</span>
          </div>
          <div className="flex justify-between">
            <span className="text-zinc-400">velocity required (×1.5 slippage):</span>
            <span className="font-mono text-orange-400">{velRequired.toFixed(3)} SOL</span>
          </div>
          <div className="border-t border-zinc-700/50 pt-1 flex justify-between">
            <span className="text-zinc-400">total required:</span>
            <span className={`font-mono font-bold ${overBudget ? 'text-red-400' : 'text-emerald-400'}`}>
              {totalRequired.toFixed(3)} SOL
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-zinc-400">wallet utilization:</span>
            <span className={`font-mono ${utilizationPct > 80 ? 'text-amber-400' : 'text-zinc-300'}`}>
              {utilizationPct.toFixed(0)}%
            </span>
          </div>
        </div>

        {/* ── Action buttons ── */}
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

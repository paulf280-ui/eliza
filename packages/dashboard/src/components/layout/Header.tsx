import { useState, useEffect } from 'react'
import { useDashboardStore } from '../../store/dashboard'
import { ConnectionDot } from '../common/ConnectionDot'
import { AddressLink } from '../common/AddressLink'
import { SolAmount } from '../common/SolAmount'

interface FngData { value: number | null; label: string }

function fngColour(value: number | null): string {
  if (value === null) return '#6b7280'
  if (value <= 25)  return '#ef4444'  // Extreme Fear — red
  if (value <= 49)  return '#f97316'  // Fear          — orange
  if (value <= 74)  return '#eab308'  // Neutral        — yellow
  if (value <= 89)  return '#22c55e'  // Greed          — green
  return '#10b981'                     // Extreme Greed  — emerald
}

export function Header() {
  const { connected, wallet } = useDashboardStore()
  const [time, setTime] = useState(new Date())
  const [fng, setFng]   = useState<FngData>({ value: null, label: '' })

  useEffect(() => {
    const id = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  // Fetch Fear & Greed on mount + every 12 hours
  useEffect(() => {
    const fetchFng = () =>
      fetch('/api/fear-greed')
        .then(r => r.ok ? r.json() : null)
        .then(d => { if (d) setFng({ value: d.value, label: d.label }) })
        .catch(() => {})
    fetchFng()
    const id = setInterval(fetchFng, 12 * 60 * 60 * 1000)
    return () => clearInterval(id)
  }, [])

  const utc = time.toISOString().slice(11, 19)

  return (
    <header className="sticky top-0 z-50">
      {/* Brand accent strip */}
      <div
        className="h-[3px] w-full"
        style={{ background: 'linear-gradient(90deg, #34d399 0%, #22d3ee 40%, #818cf8 70%, #34d399 100%)', backgroundSize: '200% 100%' }}
      />

      {/* Main header bar */}
      <div
        className="border-b border-white/[0.06] px-6 py-3"
        style={{ background: 'rgba(6, 8, 16, 0.92)', backdropFilter: 'blur(24px)' }}
      >
        <div className="flex items-center justify-between">

          {/* Left — brand identity */}
          <div className="flex items-center gap-3">
            {/* PF monogram */}
            <div
              className="flex items-center justify-center w-9 h-9 rounded-lg text-sm font-black text-zinc-900 flex-shrink-0 select-none"
              style={{ background: 'linear-gradient(135deg, #34d399, #22d3ee)', letterSpacing: '-0.05em' }}
            >
              PF
            </div>

            <div className="flex flex-col leading-none">
              <div className="flex items-center gap-2">
                <span className="text-base font-bold gradient-text tracking-tight font-mono">J.A.R.V.I.S.</span>
                <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 uppercase tracking-widest">
                  Live
                </span>
              </div>
              <span className="text-[10px] text-zinc-400 tracking-widest uppercase mt-0.5">
                PF Capital · Autonomous Solana Signal Engine
              </span>
            </div>

            <div className="flex items-center gap-1.5 ml-3 pl-3 border-l border-white/[0.06]">
              <ConnectionDot connected={connected} />
              <span className="text-xs text-slate-500">
                {connected ? 'Live' : 'Offline'}
              </span>
            </div>
          </div>

          {/* Center — UTC clock + Fear & Greed */}
          <div className="absolute left-1/2 -translate-x-1/2 flex flex-col items-center gap-0.5">
            <span className="font-mono text-sm text-slate-300 tabular-nums">{utc}</span>
            <span className="text-[10px] text-slate-600 tracking-widest uppercase">UTC</span>
            {fng.value !== null && (
              <div
                className="flex items-center gap-1 px-2 py-0.5 rounded-full mt-0.5"
                style={{
                  background: fngColour(fng.value) + '18',
                  border: `1px solid ${fngColour(fng.value)}44`,
                }}
                title="Crypto Fear & Greed Index — refreshes every 12h"
              >
                <span className="text-[9px] font-bold tabular-nums" style={{ color: fngColour(fng.value) }}>
                  {fng.value}/100
                </span>
                <span className="text-[9px] text-zinc-500 uppercase tracking-wide">
                  {fng.label}
                </span>
              </div>
            )}
          </div>

          {/* Right — wallet */}
          <div className="flex items-center gap-4">
            {wallet ? (
              <>
                <AddressLink address={wallet.address} />
                <SolAmount amount={wallet.sol_balance} size="sm" />
              </>
            ) : (
              <span className="text-xs text-slate-500">No wallet</span>
            )}
          </div>
        </div>
      </div>
    </header>
  )
}

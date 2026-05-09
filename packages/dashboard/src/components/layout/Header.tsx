import { useState, useEffect, useCallback } from 'react'
import { useDashboardStore } from '../../store/dashboard'
import { ConnectionDot } from '../common/ConnectionDot'
import { AddressLink } from '../common/AddressLink'
import { SolAmount } from '../common/SolAmount'

declare global {
  interface Window {
    solana?: {
      isPhantom: boolean
      publicKey: { toString(): string } | null
      connect(): Promise<{ publicKey: { toString(): string } }>
      disconnect(): Promise<void>
    }
  }
}

async function fetchSolBalance(address: string): Promise<number | null> {
  try {
    const res = await fetch('https://api.mainnet-beta.solana.com', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'getBalance', params: [address, { commitment: 'confirmed' }] }),
    })
    const data = await res.json()
    return typeof data.result?.value === 'number' ? data.result.value / 1e9 : null
  } catch { return null }
}

export function Header() {
  const { connected, wallet } = useDashboardStore()
  const [time, setTime] = useState(new Date())
  const [phantom, setPhantom] = useState<{ address: string; balance: number | null } | null>(null)
  const [phantomConnecting, setPhantomConnecting] = useState(false)
  const [hasPhantom, setHasPhantom] = useState(false)

  useEffect(() => {
    const id = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  // Detect Phantom and auto-connect if already authorised
  useEffect(() => {
    const tryAutoConnect = async () => {
      if (!window.solana?.isPhantom) return
      setHasPhantom(true)
      if (window.solana.publicKey) {
        const address = window.solana.publicKey.toString()
        const balance = await fetchSolBalance(address)
        setPhantom({ address, balance })
      }
    }
    // Extension may not be injected yet on first render
    const t = setTimeout(tryAutoConnect, 500)
    return () => clearTimeout(t)
  }, [])

  // Refresh Phantom balance every 30s when connected
  useEffect(() => {
    if (!phantom?.address) return
    const id = setInterval(async () => {
      const balance = await fetchSolBalance(phantom.address)
      setPhantom(prev => prev ? { ...prev, balance } : null)
    }, 30_000)
    return () => clearInterval(id)
  }, [phantom?.address])

  const connectPhantom = useCallback(async () => {
    if (!window.solana?.isPhantom || phantomConnecting) return
    setPhantomConnecting(true)
    try {
      const { publicKey } = await window.solana.connect()
      const address = publicKey.toString()
      const balance = await fetchSolBalance(address)
      setPhantom({ address, balance })
    } catch { /* user rejected */ }
    finally { setPhantomConnecting(false) }
  }, [phantomConnecting])

  const refreshPhantom = useCallback(async () => {
    if (!phantom?.address) return
    const balance = await fetchSolBalance(phantom.address)
    setPhantom(prev => prev ? { ...prev, balance } : null)
  }, [phantom?.address])

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

          {/* Center — UTC clock */}
          <div className="absolute left-1/2 -translate-x-1/2 flex flex-col items-center">
            <span className="font-mono text-sm text-slate-300 tabular-nums">{utc}</span>
            <span className="text-[10px] text-slate-600 tracking-widest uppercase">UTC</span>
          </div>

          {/* Right — bot wallet + Phantom wallet */}
          <div className="flex items-center gap-4">
            {/* Bot wallet */}
            {wallet ? (
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-zinc-600 uppercase tracking-wider">bot</span>
                <AddressLink address={wallet.address} />
                <SolAmount amount={wallet.sol_balance} size="sm" />
              </div>
            ) : (
              <span className="text-xs text-slate-500">No wallet</span>
            )}

            {/* Divider */}
            {(phantom || hasPhantom) && (
              <div className="w-px h-4 bg-zinc-700" />
            )}

            {/* Phantom wallet */}
            {phantom ? (
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-zinc-600 uppercase tracking-wider">phantom</span>
                <span className="text-xs font-mono text-zinc-300">
                  {phantom.address.slice(0, 4)}…{phantom.address.slice(-4)}
                </span>
                {phantom.balance != null ? (
                  <span className="text-sm font-bold font-mono text-amber-400">
                    {phantom.balance.toFixed(3)} SOL
                  </span>
                ) : (
                  <span className="text-xs text-zinc-500">—</span>
                )}
                <button
                  onClick={refreshPhantom}
                  className="text-zinc-600 hover:text-zinc-300 transition-colors text-xs"
                  title="Refresh balance"
                >
                  ↻
                </button>
              </div>
            ) : hasPhantom ? (
              <button
                onClick={connectPhantom}
                disabled={phantomConnecting}
                className="text-xs px-3 py-1 rounded-md font-semibold transition-all disabled:opacity-50"
                style={{ background: 'rgba(147,51,234,0.15)', border: '1px solid rgba(147,51,234,0.4)', color: '#c084fc' }}
              >
                {phantomConnecting ? '…' : 'Connect Phantom'}
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </header>
  )
}

import { useState } from 'react'
import { GlassCard } from '../common/GlassCard'
import { useDashboardStore } from '../../store/dashboard'

const PHANTOM_BASE = 'https://trade.phantom.com'
const POPUP_OPTS = 'width=1400,height=900,left=80,top=60,resizable=yes,scrollbars=yes'

function openPhantom(mint?: string) {
  const url = mint
    ? `${PHANTOM_BASE}/sol/${mint}`
    : PHANTOM_BASE
  window.open(url, 'phantom_terminal', POPUP_OPTS)
}

export default function PhantomTerminalPanel() {
  const { positions, wallet } = useDashboardStore()
  const [mintInput, setMintInput] = useState('')

  const openPositions = Object.values(positions).sort((a, b) => b.pnl_pct - a.pnl_pct)

  return (
    <GlassCard className="h-full flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-shrink-0">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-400 font-semibold">Phantom Terminal</div>
          <div className="text-zinc-500 text-xs mt-0.5">opens in popup · wallet auto-connects · top holders · chart</div>
        </div>
        <button
          onClick={() => openPhantom()}
          className="flex items-center gap-2 text-sm px-4 py-2 rounded-lg font-semibold transition-all hover:opacity-90 active:scale-95"
          style={{ background: 'rgba(147,51,234,0.2)', border: '1px solid rgba(147,51,234,0.5)', color: '#c084fc' }}
        >
          <span>👻</span>
          Open Phantom Terminal
        </button>
      </div>

      {/* Manual mint input */}
      <div className="flex gap-2 flex-shrink-0">
        <input
          type="text"
          placeholder="Paste token mint address to open specific chart…"
          value={mintInput}
          onChange={e => setMintInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && mintInput.trim()) openPhantom(mintInput.trim()) }}
          className="flex-1 bg-zinc-800/60 border border-zinc-700/50 rounded-md px-3 py-2 text-xs font-mono text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-purple-500/50"
        />
        <button
          onClick={() => { if (mintInput.trim()) openPhantom(mintInput.trim()) }}
          disabled={!mintInput.trim()}
          className="px-4 py-2 rounded-md text-xs font-semibold disabled:opacity-30 transition-all"
          style={{ background: 'rgba(147,51,234,0.15)', border: '1px solid rgba(147,51,234,0.35)', color: '#c084fc' }}
        >
          Open ↗
        </button>
        {mintInput && (
          <button onClick={() => setMintInput('')} className="text-zinc-600 hover:text-zinc-400 text-xs px-2 transition-colors">✕</button>
        )}
      </div>

      {/* Open positions — quick links */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {openPositions.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-4 py-8">
            <div className="text-zinc-700 text-4xl">👻</div>
            <div className="text-zinc-600 text-sm text-center">
              No open positions right now.<br />
              <span className="text-zinc-700 text-xs">Click a position below when the bot enters a trade to jump straight to its chart.</span>
            </div>
            {wallet && (
              <button
                onClick={() => openPhantom()}
                className="text-xs px-3 py-1.5 rounded-md transition-all"
                style={{ background: 'rgba(147,51,234,0.12)', border: '1px solid rgba(147,51,234,0.3)', color: '#a855f7' }}
              >
                View wallet in Phantom Terminal ↗
              </button>
            )}
          </div>
        ) : (
          <div>
            <div className="text-[10px] text-zinc-600 uppercase tracking-widest mb-2 px-1">
              Open positions — click to view chart
            </div>
            <div className="grid grid-cols-2 xl:grid-cols-3 gap-2">
              {openPositions.map(pos => {
                const pnlPos = pos.pnl_pct >= 0
                const ageMins = Math.floor((pos.age_seconds ?? 0) / 60)
                return (
                  <button
                    key={pos.mint}
                    onClick={() => openPhantom(pos.mint)}
                    className="text-left px-3 py-2.5 rounded-lg border transition-all hover:border-purple-500/40 hover:bg-purple-500/5 active:scale-[0.98] group"
                    style={{ borderColor: 'rgba(82,82,91,0.4)', background: 'rgba(24,24,27,0.5)' }}
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[10px] font-mono text-zinc-400 truncate group-hover:text-purple-300 transition-colors">
                        {pos.mint.slice(0, 8)}…
                      </span>
                      <span className={`text-xs font-bold font-mono ${pnlPos ? 'text-emerald-400' : 'text-red-400'}`}>
                        {pnlPos ? '+' : ''}{pos.pnl_pct.toFixed(1)}%
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[9px] text-zinc-600">{pos.dex}</span>
                      <span className="text-[9px] text-zinc-600">{ageMins}m old</span>
                    </div>
                    <div className="flex items-center justify-between mt-1">
                      <span className="text-[9px] text-zinc-500">{pos.entry_sol_spent.toFixed(3)} SOL in</span>
                      <span className="text-[9px] text-purple-500 opacity-0 group-hover:opacity-100 transition-opacity">open chart ↗</span>
                    </div>
                  </button>
                )
              })}
            </div>
          </div>
        )}
      </div>
    </GlassCard>
  )
}

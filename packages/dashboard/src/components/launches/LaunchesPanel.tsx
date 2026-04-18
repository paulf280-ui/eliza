import { useState } from 'react'
import { useDashboardStore } from '../../store/dashboard'
import { GlassCard } from '../common/GlassCard'
import { Badge } from '../common/Badge'

function relativeTime(ts: number): string {
  const diff = Math.floor((Date.now() / 1000) - ts)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  return `${Math.floor(diff / 3600)}h ago`
}

export default function LaunchesPanel() {
  const { recentLaunches, connected } = useDashboardStore()
  const [buyingMint, setBuyingMint] = useState<string | null>(null)
  const [buyAmount, setBuyAmount] = useState('0.01')

  const handleBuy = async (mint: string) => {
    if (!connected) return
    setBuyingMint(mint)
    try {
      const res = await fetch('/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: `buy ${buyAmount} SOL of ${mint}`,
          user_id: 'dashboard',
          room_id: 'dashboard',
        }),
      })
      const data = await res.json()
      console.log('Buy preview:', data.text)
    } catch (err) {
      console.error('Buy request failed:', err)
    } finally {
      setBuyingMint(null)
    }
  }

  return (
    <GlassCard className="flex flex-col overflow-hidden">
      <div className="flex items-center justify-between mb-3 flex-shrink-0">
        <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">Recent Launches</span>
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-600">SOL:</span>
          <input
            type="number"
            value={buyAmount}
            onChange={e => setBuyAmount(e.target.value)}
            min="0.005"
            step="0.005"
            className="w-16 bg-zinc-800 border border-zinc-700 rounded px-2 py-0.5 text-xs font-mono text-zinc-200 focus:outline-none focus:border-emerald-500/50"
          />
        </div>
      </div>

      <div className="overflow-y-auto flex-1 space-y-2 pr-1 custom-scrollbar">
        {recentLaunches.length === 0 ? (
          <div className="text-center py-6 text-zinc-700 text-xs">No recent launches</div>
        ) : (
          recentLaunches.map((launch) => (
            <div
              key={launch.mint}
              className="flex items-center gap-2 p-2 rounded-lg bg-zinc-800/40 hover:bg-zinc-800/70 border border-zinc-700/30 transition-all group"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <a
                    href={`https://pump.fun/${launch.mint}`}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-xs text-zinc-300 hover:text-cyan-400 transition-colors"
                  >
                    {launch.mint.slice(0, 8)}…{launch.mint.slice(-4)}
                  </a>
                  {launch.complete && <Badge variant="green">graduated</Badge>}
                </div>
                {/* Progress bar */}
                <div className="h-1 bg-zinc-700 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-emerald-500 to-cyan-500 rounded-full transition-all"
                    style={{ width: `${Math.min(launch.progress_pct, 100)}%` }}
                  />
                </div>
                <div className="flex justify-between text-xs text-zinc-600 mt-0.5">
                  <span>{launch.progress_pct.toFixed(1)}% filled</span>
                  <span>{relativeTime(launch.timestamp)}</span>
                </div>
              </div>
              <button
                onClick={() => handleBuy(launch.mint)}
                disabled={!connected || buyingMint === launch.mint}
                className="flex-shrink-0 px-2 py-1 rounded text-xs font-semibold bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30 border border-emerald-500/30 transition-all disabled:opacity-40 disabled:cursor-not-allowed opacity-0 group-hover:opacity-100"
              >
                {buyingMint === launch.mint ? '…' : 'Buy'}
              </button>
            </div>
          ))
        )}
      </div>
    </GlassCard>
  )
}

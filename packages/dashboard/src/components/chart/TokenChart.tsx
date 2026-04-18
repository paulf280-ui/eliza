import { useDashboardStore } from '../../store/dashboard'
import { GlassCard } from '../common/GlassCard'
import StrategyPanel from './StrategyPanel'

export default function TokenChart() {
  const { selectedMint, positions } = useDashboardStore()
  const pos = selectedMint ? positions[selectedMint] : null

  const embedUrl = selectedMint
    ? `https://dexscreener.com/solana/${selectedMint}?embed=1&theme=dark&trades=0&info=0`
    : null

  return (
    <GlassCard className="flex flex-col flex-1 min-h-0">
      {embedUrl ? (
        <>
          <div className="flex items-center justify-between mb-2 flex-shrink-0">
            <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">
              Token Chart
            </span>
            {pos && (
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs text-zinc-300">
                  {selectedMint!.slice(0, 6)}…{selectedMint!.slice(-4)}
                </span>
                <span className={`text-xs font-semibold ${pos.pnl_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {pos.pnl_pct >= 0 ? '+' : ''}{pos.pnl_pct.toFixed(1)}%
                </span>
                <a
                  href={`https://solscan.io/token/${selectedMint}`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-zinc-600 hover:text-zinc-400 transition-colors text-xs"
                  title="View on Solscan"
                >
                  ↗
                </a>
              </div>
            )}
          </div>
          <iframe
            key={selectedMint}
            src={embedUrl}
            className="flex-1 w-full rounded-lg border-0 min-h-[240px]"
            allow="clipboard-write"
            title="Token chart"
          />
        </>
      ) : (
        <StrategyPanel />
      )}
    </GlassCard>
  )
}

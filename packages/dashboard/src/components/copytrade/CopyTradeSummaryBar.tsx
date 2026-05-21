interface Props {
  balance: number
  netPnl: number
  trades: number
  wins: number
  openCount: number
  maxSlots?: number
  signalsToday: number
  watchedWallets: string[]
  tradeSize?: number
  liveMode?: boolean
  paperMode?: boolean
  paused?: boolean
  pauseLoading?: boolean
  onTogglePause?: () => void
}

export default function CopyTradeSummaryBar({
  balance, netPnl, trades, wins, openCount, maxSlots, signalsToday, watchedWallets, tradeSize, liveMode,
  paperMode, paused, pauseLoading, onTogglePause,
}: Props) {
  const wr = trades > 0 ? Math.round((wins / trades) * 100) : 0
  const pnlPos = netPnl >= 0

  return (
    <div
      className="col-span-12 rounded-xl px-5 py-3 flex flex-wrap items-center justify-between gap-4"
      style={{ background: 'rgba(249,115,22,0.06)', border: '1px solid rgba(249,115,22,0.2)' }}
    >
      {/* Left: title + pause button */}
      <div className="flex items-center gap-3">
        <div className="w-2.5 h-2.5 rounded-full bg-orange-400 animate-pulse"
          style={{ boxShadow: '0 0 8px rgba(249,115,22,0.8)' }} />
        <span className="text-sm font-bold text-zinc-200 uppercase tracking-widest">
          Monster Command Centre
        </span>
        {liveMode && (
          <span className="text-[10px] font-bold px-2 py-0.5 rounded-full"
            style={{ background: 'rgba(239,68,68,0.2)', color: '#f87171' }}>
            LIVE
          </span>
        )}
        {paperMode && !liveMode && (
          <span className="text-[10px] font-bold px-2 py-0.5 rounded-full animate-pulse"
            style={{ background: 'rgba(251,191,36,0.15)', border: '1px solid rgba(251,191,36,0.4)', color: '#fbbf24' }}>
            PAPER
          </span>
        )}
        {paused && (
          <span className="text-[10px] font-bold text-amber-400 animate-pulse">⏸ PAUSED</span>
        )}

        {/* Pause / Resume — always visible emergency control */}
        <button
          onClick={onTogglePause}
          disabled={pauseLoading}
          className="flex items-center gap-1.5 px-3 py-1 rounded-lg font-bold text-xs tracking-wide transition-all duration-150 disabled:opacity-50 ml-1"
          style={paused ? {
            background: 'rgba(52,211,153,0.15)',
            border: '1px solid rgba(52,211,153,0.5)',
            color: '#6ee7b7',
          } : {
            background: 'rgba(239,68,68,0.12)',
            border: '1px solid rgba(239,68,68,0.4)',
            color: '#f87171',
          }}
        >
          {pauseLoading
            ? <span className="animate-spin">⟳</span>
            : paused ? <>▶ RESUME</> : <>⏸ PAUSE</>}
        </button>
      </div>

      {/* Stats */}
      <div className="flex flex-wrap items-center gap-6">
        <Stat label={paperMode && !liveMode ? 'Paper Balance' : 'Balance'} value={`${balance.toFixed(3)} SOL`} valueClass="text-amber-400" />
        <Stat
          label="Net P&L"
          value={`${pnlPos ? '+' : ''}${netPnl.toFixed(4)} SOL`}
          valueClass={pnlPos ? 'text-emerald-400' : 'text-red-400'}
        />
        {trades > 0 && (
          <Stat label="Win Rate" value={`${wr}% (${wins}/${trades})`}
            valueClass={wr >= 50 ? 'text-emerald-400' : 'text-amber-400'} />
        )}
        <Stat label="Open Slots" value={`${openCount}/${maxSlots ?? 2}`} valueClass="text-orange-300" />
        {tradeSize != null && (
          <Stat label="Trade Size" value={`${tradeSize.toFixed(3)} SOL`} valueClass="text-orange-400" />
        )}
        <Stat label="Signals Today" value={String(signalsToday)} valueClass="text-zinc-300" />
      </div>
    </div>
  )
}

function Stat({ label, value, valueClass, title }: { label: string; value: string; valueClass: string; title?: string }) {
  return (
    <div className="flex flex-col items-center gap-0.5" title={title}>
      <span className="text-[10px] text-zinc-600 uppercase tracking-widest">{label}</span>
      <span className={`text-sm font-bold font-mono ${valueClass}`}>{value}</span>
    </div>
  )
}

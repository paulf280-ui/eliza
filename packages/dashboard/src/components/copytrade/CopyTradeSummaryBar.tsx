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
}

export default function CopyTradeSummaryBar({
  balance, netPnl, trades, wins, openCount, maxSlots, signalsToday, watchedWallets, tradeSize, liveMode
}: Props) {
  const wr = trades > 0 ? Math.round((wins / trades) * 100) : 0
  const pnlPos = netPnl >= 0

  return (
    <div
      className="col-span-12 rounded-xl px-5 py-3 flex flex-wrap items-center justify-between gap-4"
      style={{ background: 'rgba(249,115,22,0.06)', border: '1px solid rgba(249,115,22,0.2)' }}
    >
      {/* Left: title */}
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
      </div>

      {/* Stats */}
      <div className="flex flex-wrap items-center gap-6">
        <Stat label="Balance" value={`${balance.toFixed(3)} SOL`} valueClass="text-amber-400" />
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
          <Stat label="Trade Size" value={`${tradeSize.toFixed(3)} SOL`} valueClass="text-orange-400"
            title="Compounding: 20% of balance ÷ 5 slots" />
        )}
        <Stat label="Signals Today" value={String(signalsToday)} valueClass="text-zinc-300" />
        <Stat label="Watching" value={`${watchedWallets.length} wallets`} valueClass="text-zinc-400" />
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

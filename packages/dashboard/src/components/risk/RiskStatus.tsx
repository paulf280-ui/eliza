import { useDashboardStore } from '../../store/dashboard'
import { GlassCard } from '../common/GlassCard'
import { fetchControl } from '../../api/client'

export default function RiskStatus() {
  const { risk, config, connected } = useDashboardStore()

  if (!risk) return null

  const dailyLossPct = risk.daily_pnl_sol < 0 && config
    ? Math.abs(risk.daily_pnl_sol) / (config.max_daily_loss_pct / 100) / 100 * 100
    : 0
  const lossPctOfLimit = config ? Math.min((Math.abs(risk.daily_pnl_sol) / (config.max_daily_loss_pct / 100 * 100)) * 100, 100) : 0

  const handlePause = async () => {
    if (!connected) return
    await fetchControl(risk.circuit_broken ? 'resume' : 'pause')
  }

  const handleReset = async () => {
    if (!connected) return
    await fetchControl('reset_cb')
  }

  return (
    <GlassCard className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">Risk Management</span>
        <div className="flex gap-2">
          <button
            onClick={handlePause}
            disabled={!connected}
            className={`px-3 py-1 rounded text-xs font-semibold transition-all ${
              risk.circuit_broken
                ? 'bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30 border border-emerald-500/40'
                : 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30 border border-amber-500/40'
            } disabled:opacity-40 disabled:cursor-not-allowed`}
          >
            {risk.circuit_broken ? 'Resume Trading' : 'Pause Trading'}
          </button>
          {risk.circuit_broken && (
            <button
              onClick={handleReset}
              disabled={!connected}
              className="px-3 py-1 rounded text-xs font-semibold bg-zinc-700/60 text-zinc-300 hover:bg-zinc-700 border border-zinc-600 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
            >
              Reset
            </button>
          )}
        </div>
      </div>

      {/* Daily loss bar */}
      <div>
        <div className="flex justify-between text-xs mb-1">
          <span className="text-zinc-400">Daily P&L</span>
          <span className={risk.daily_pnl_sol >= 0 ? 'text-emerald-400' : 'text-red-400'}>
            {risk.daily_pnl_sol >= 0 ? '+' : ''}{risk.daily_pnl_sol.toFixed(4)} SOL
          </span>
        </div>
        <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${
              risk.daily_pnl_sol >= 0 ? 'bg-emerald-500' : 'bg-red-500'
            }`}
            style={{ width: `${Math.min(lossPctOfLimit, 100)}%` }}
          />
        </div>
        <div className="flex justify-between text-xs mt-1 text-zinc-600">
          <span>0</span>
          <span>{config ? `${config.max_daily_loss_pct}% limit` : ''}</span>
        </div>
      </div>

      {/* Consecutive losses */}
      <div>
        <div className="flex items-center justify-between">
          <span className="text-xs text-zinc-400">Consecutive Losses</span>
          <div className="flex gap-1.5">
            {Array.from({ length: config?.max_consecutive_losses ?? 3 }).map((_, i) => (
              <div
                key={i}
                className={`w-3 h-3 rounded-full transition-all ${
                  i < risk.consecutive_losses ? 'bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.8)]' : 'bg-zinc-700'
                }`}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Position count */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-zinc-400">Open Positions</span>
        <span className="font-mono text-zinc-200">
          {risk.open_position_count}
          <span className="text-zinc-600"> / {config?.max_concurrent_positions ?? 6}</span>
        </span>
      </div>

      {/* TP summary */}
      {config && (
        <div className="pt-2 border-t border-zinc-800 grid grid-cols-4 gap-1 text-xs">
          {[
            { label: `TP1 +${Math.round((config.tp1_mult - 1) * 100)}%`, color: 'text-emerald-400' },
            { label: `TP2 +${Math.round((config.tp2_mult - 1) * 100)}%`, color: 'text-emerald-300' },
            { label: `TP3 +${Math.round((config.tp3_mult - 1) * 100)}%`, color: 'text-cyan-400' },
            { label: `SL -${Math.round(config.stop_loss_pct * 100)}%`, color: 'text-red-400' },
          ].map(({ label, color }) => (
            <div key={label} className={`text-center font-mono ${color} bg-zinc-800/60 rounded py-1`}>
              {label}
            </div>
          ))}
        </div>
      )}
    </GlassCard>
  )
}

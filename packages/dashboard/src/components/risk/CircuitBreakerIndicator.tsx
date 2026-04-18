interface CircuitBreakerIndicatorProps {
  active: boolean
}

export function CircuitBreakerIndicator({ active }: CircuitBreakerIndicatorProps) {
  if (active) {
    return (
      <div className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-semibold bg-red-500/15 text-red-400 shadow-lg shadow-red-500/20">
        <span className="w-2 h-2 rounded-full bg-red-400" />
        PAUSED
      </div>
    )
  }
  return (
    <div className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-semibold bg-emerald-500/15 text-emerald-400 shadow-lg shadow-emerald-500/20">
      <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
      ACTIVE
    </div>
  )
}

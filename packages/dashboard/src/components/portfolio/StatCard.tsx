interface StatCardProps {
  title: string
  value: string
  subtitle?: string
  trend?: number
  valueClassName?: string
}

export function StatCard({ title, value, subtitle, trend, valueClassName = '' }: StatCardProps) {
  return (
    <div className="glass-card p-4">
      <div className="text-xs text-slate-400 uppercase tracking-wider mb-1">{title}</div>
      <div className={`text-2xl font-semibold font-mono ${valueClassName}`}>{value}</div>
      <div className="flex items-center gap-2 mt-1">
        {subtitle && <span className="text-sm text-slate-500">{subtitle}</span>}
        {trend !== undefined && (
          <span className={`text-xs font-medium flex items-center gap-0.5 ${trend >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            <svg className={`w-3 h-3 ${trend < 0 ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
            </svg>
            {Math.abs(trend).toFixed(1)}%
          </span>
        )}
      </div>
    </div>
  )
}

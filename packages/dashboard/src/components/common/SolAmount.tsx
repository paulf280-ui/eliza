const sizes = { sm: 'text-xs', md: 'text-sm', lg: 'text-lg' } as const

interface SolAmountProps {
  amount: number
  showSign?: boolean
  size?: keyof typeof sizes
}

export function SolAmount({ amount, showSign = false, size = 'md' }: SolAmountProps) {
  const color = amount >= 0 ? 'text-emerald-400' : 'text-red-400'
  const prefix = showSign && amount > 0 ? '+' : ''
  return (
    <span className={`font-mono ${sizes[size]} ${color}`}>
      {prefix}{amount.toFixed(4)}
      <span className="text-slate-500 ml-1">SOL</span>
    </span>
  )
}

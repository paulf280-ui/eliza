const variants = {
  green: 'bg-emerald-500/10 text-emerald-400',
  red: 'bg-red-500/10 text-red-400',
  amber: 'bg-amber-500/10 text-amber-400',
  blue: 'bg-blue-500/10 text-blue-400',
  gray: 'bg-gray-500/10 text-gray-400',
} as const

interface BadgeProps {
  children: React.ReactNode
  variant: keyof typeof variants
}

export function Badge({ children, variant }: BadgeProps) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${variants[variant]}`}>
      {children}
    </span>
  )
}

import type { CSSProperties } from 'react'

interface GlassCardProps {
  children: React.ReactNode
  className?: string
  style?: CSSProperties
}

export function GlassCard({ children, className = '', style }: GlassCardProps) {
  return <div className={`glass-card p-4 ${className}`} style={style}>{children}</div>
}

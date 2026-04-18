interface ConnectionDotProps {
  connected: boolean
}

export function ConnectionDot({ connected }: ConnectionDotProps) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${
        connected ? 'bg-emerald-400 animate-pulse' : 'bg-red-400'
      }`}
    />
  )
}

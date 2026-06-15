import { useState } from 'react'
import { GlassCard } from '../common/GlassCard'

// The trading bot dashboard now embeds the SAME polished Cabal-Hunter map that
// powers the public SaaS (api.cabal-hunter.com/map) — one renderer, one place to
// maintain, and the operator sees exactly what customers see: score gauge,
// funding-trace bubbles, deployer history, cohort PnL, CEX funding, LP "the
// house" labelling, etc. The old hand-rolled SVG bubble map is retired.

const MAP_BASE = 'https://api.cabal-hunter.com/map'

export default function BubbleMapPanel({ mint, tokenName }: { mint: string; tokenName?: string }) {
  const [loaded, setLoaded] = useState(false)
  if (!mint) return null
  const src = `${MAP_BASE}?mint=${encodeURIComponent(mint)}`

  return (
    <GlassCard className="flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold text-zinc-400 uppercase tracking-widest">
            Cabal-Hunter Map
          </span>
          {tokenName && <span className="text-xs text-zinc-500">{tokenName}</span>}
        </div>
        <a
          href={src}
          target="_blank"
          rel="noopener noreferrer"
          className="text-[10px] font-semibold text-orange-400 hover:text-orange-300 transition-colors"
        >
          Open full screen ↗
        </a>
      </div>

      {/* Embedded live map — the exact public tool */}
      <div
        className="relative w-full rounded-lg overflow-hidden"
        style={{ height: 600, border: '1px solid rgba(255,255,255,0.06)', background: 'rgba(0,0,0,0.3)' }}
      >
        {!loaded && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 text-zinc-500 text-sm">
            <div className="w-3 h-3 rounded-full bg-orange-400 animate-bounce" />
            Loading live holder map…
          </div>
        )}
        <iframe
          key={mint}
          src={src}
          title="Cabal-Hunter holder map"
          onLoad={() => setLoaded(true)}
          loading="lazy"
          style={{ width: '100%', height: '100%', border: 0, display: 'block' }}
        />
      </div>
    </GlassCard>
  )
}

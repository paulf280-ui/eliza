import { useEffect, useRef, useState, useMemo, type ReactElement } from 'react'
import { GlassCard } from '../common/GlassCard'

// ── Types ─────────────────────────────────────────────────────────────────────

interface Holder {
  rank: number
  address: string
  address_short: string
  ui_amount: number
  pct: number
  cluster_id: number | null
  is_lp: boolean
  label: string | null
}

interface Cluster {
  id: number
  master_short: string
  wallet_count: number
  combined_pct: number
  risk: 'HIGH' | 'MEDIUM' | 'CLEAN'
}

interface ClusterMapData {
  risk: 'HIGH' | 'MEDIUM' | 'CLEAN'
  holders: Holder[]
  clusters: Cluster[]
  total_supply_est: number
  wallets_checked: number
  skip_reason: string | null
  computed_at: number
}

// ── Colour palette for clusters ───────────────────────────────────────────────

const CLUSTER_COLOURS = [
  '#ef4444', // red   — cluster 0
  '#f97316', // orange
  '#eab308', // amber
  '#22c55e', // green
  '#3b82f6', // blue
  '#a855f7', // purple
  '#ec4899', // pink
]
const LP_COLOUR        = '#374151'  // gray-700
const SOLO_COLOUR      = '#3f3f46'  // zinc-700
const RISK_COLOURS     = { HIGH: '#ef4444', MEDIUM: '#f59e0b', CLEAN: '#22c55e' }

function clusterColour(id: number | null): string {
  if (id === null) return SOLO_COLOUR
  return CLUSTER_COLOURS[id % CLUSTER_COLOURS.length]
}

// ── Simple force-directed layout (pure JS, no D3) ─────────────────────────────

interface BubbleNode {
  holder: Holder
  r: number        // radius
  x: number
  y: number
  vx: number
  vy: number
}

function computeLayout(holders: Holder[], W: number, H: number): BubbleNode[] {
  const cx = W / 2
  const cy = H / 2
  const maxR = 48
  const maxPct = Math.max(...holders.map(h => h.pct), 1)

  // Build nodes — radius proportional to sqrt(pct)
  const nodes: BubbleNode[] = holders.map((h, i) => {
    const r = Math.max(6, Math.sqrt(h.pct / maxPct) * maxR)
    // Seed positions in a spiral so clusters start near each other
    const angle = (i / holders.length) * 2 * Math.PI
    const dist  = Math.min(W, H) * 0.28
    return {
      holder: h,
      r,
      x: cx + dist * Math.cos(angle) + (Math.random() - 0.5) * 20,
      y: cy + dist * Math.sin(angle) + (Math.random() - 0.5) * 20,
      vx: 0,
      vy: 0,
    }
  })

  // Group cluster members' target positions
  const clusterCentres: Record<number, { x: number; y: number }> = {}
  const clusterIds = [...new Set(holders.map(h => h.cluster_id).filter(id => id !== null))] as number[]
  clusterIds.forEach((id, idx) => {
    const angle = (idx / Math.max(clusterIds.length, 1)) * 2 * Math.PI
    clusterCentres[id] = {
      x: cx + Math.min(W, H) * 0.22 * Math.cos(angle + Math.PI / 4),
      y: cy + Math.min(W, H) * 0.22 * Math.sin(angle + Math.PI / 4),
    }
  })

  const DAMPING   = 0.8
  const REPULSION = 1.2
  const GRAVITY   = 0.015
  const CLUSTER_K = 0.06

  for (let iter = 0; iter < 280; iter++) {
    // Reset forces
    nodes.forEach(n => { n.vx *= DAMPING; n.vy *= DAMPING })

    // Pairwise repulsion
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j]
        const dx   = b.x - a.x || 0.01
        const dy   = b.y - a.y || 0.01
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.01
        const minD = a.r + b.r + 3
        if (dist < minD * 1.8) {
          const force = REPULSION * (minD - dist) / dist
          a.vx -= force * dx; a.vy -= force * dy
          b.vx += force * dx; b.vy += force * dy
        }
      }
      // Gravity toward centre
      const n = nodes[i]
      n.vx += (cx - n.x) * GRAVITY
      n.vy += (cy - n.y) * GRAVITY
      // Cluster attraction
      const cid = n.holder.cluster_id
      if (cid !== null && clusterCentres[cid]) {
        n.vx += (clusterCentres[cid].x - n.x) * CLUSTER_K
        n.vy += (clusterCentres[cid].y - n.y) * CLUSTER_K
      }
    }

    // Integrate
    nodes.forEach(n => {
      n.x = Math.max(n.r + 4, Math.min(W - n.r - 4, n.x + n.vx))
      n.y = Math.max(n.r + 4, Math.min(H - n.r - 4, n.y + n.vy))
    })
  }

  return nodes
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function BubbleMapPanel({ mint, tokenName }: { mint: string; tokenName?: string }) {
  const [data, setData]       = useState<ClusterMapData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState<string | null>(null)
  const [tooltip, setTooltip] = useState<{ node: BubbleNode; mx: number; my: number } | null>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const W = 520
  const H = 300

  useEffect(() => {
    if (!mint) return
    setLoading(true)
    setError(null)
    setData(null)
    fetch(`/api/cluster-map?mint=${encodeURIComponent(mint)}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
      .then(d => { setData(d); setLoading(false) })
      .catch(e => { setError(String(e)); setLoading(false) })
  }, [mint])

  const nodes = useMemo<BubbleNode[]>(() => {
    if (!data?.holders?.length) return []
    return computeLayout(data.holders, W, H)
  }, [data])

  const riskColour = data ? RISK_COLOURS[data.risk] : '#6b7280'

  return (
    <GlassCard className="flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold text-zinc-400 uppercase tracking-widest">
            Holder Bubble Map
          </span>
          {tokenName && (
            <span className="text-xs text-zinc-500">{tokenName}</span>
          )}
        </div>
        {data && (
          <div className="flex items-center gap-3">
            <span className="text-[10px] text-zinc-600">
              {data.holders.length} holders · {data.wallets_checked} traced
            </span>
            <span
              className="text-[10px] font-bold px-2 py-0.5 rounded-full"
              style={{ background: riskColour + '22', color: riskColour, border: `1px solid ${riskColour}55` }}
            >
              {data.risk}
            </span>
          </div>
        )}
      </div>

      {/* Loading */}
      {loading && (
        <div className="flex items-center justify-center h-[200px] gap-2 text-zinc-500 text-sm">
          <div className="w-3 h-3 rounded-full bg-orange-400 animate-bounce" />
          Tracing holder funding sources…
        </div>
      )}

      {/* Error */}
      {error && !loading && (
        <div className="flex items-center justify-center h-[150px] text-red-400 text-xs">
          Failed to load cluster map: {error}
        </div>
      )}

      {/* SVG Bubble Map */}
      {!loading && !error && nodes.length > 0 && (
        <div className="relative">
          <svg
            ref={svgRef}
            width="100%"
            viewBox={`0 0 ${W} ${H}`}
            className="rounded-lg overflow-visible"
            style={{ background: 'rgba(0,0,0,0.25)', border: '1px solid rgba(255,255,255,0.04)' }}
            onMouseLeave={() => setTooltip(null)}
          >
            {/* Cluster connection arcs */}
            {data?.clusters.map(cluster => {
              const members = nodes.filter(n => n.holder.cluster_id === cluster.id)
              if (members.length < 2) return null
              const lines: ReactElement[] = []
              for (let i = 0; i < members.length; i++) {
                for (let j = i + 1; j < members.length; j++) {
                  const a = members[i], b = members[j]
                  lines.push(
                    <line key={`${i}-${j}`}
                      x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                      stroke={CLUSTER_COLOURS[cluster.id % CLUSTER_COLOURS.length]}
                      strokeOpacity={0.3}
                      strokeWidth={1.5}
                      strokeDasharray="4 3"
                    />
                  )
                }
              }
              return <g key={cluster.id}>{lines}</g>
            })}

            {/* Bubbles */}
            {nodes.map(node => {
              const h   = node.holder
              const col = h.is_lp ? LP_COLOUR : clusterColour(h.cluster_id)
              const isHigh = data?.clusters.find(c => c.id === h.cluster_id)?.risk === 'HIGH'
              return (
                <g key={h.address}
                  onMouseEnter={e => setTooltip({ node, mx: node.x, my: node.y })}
                  onMouseLeave={() => setTooltip(null)}
                  style={{ cursor: 'pointer' }}
                >
                  {/* Outer ring for clustered wallets */}
                  {h.cluster_id !== null && (
                    <circle
                      cx={node.x} cy={node.y} r={node.r + 3}
                      fill="none"
                      stroke={col}
                      strokeOpacity={isHigh ? 0.9 : 0.5}
                      strokeWidth={isHigh ? 2 : 1}
                    />
                  )}
                  {/* Main bubble */}
                  <circle
                    cx={node.x} cy={node.y} r={node.r}
                    fill={col}
                    fillOpacity={h.is_lp ? 0.25 : 0.55}
                    stroke={col}
                    strokeWidth={1}
                    strokeOpacity={0.7}
                  />
                  {/* Label inside bubble if big enough */}
                  {node.r >= 14 && (
                    <text
                      x={node.x} y={node.y}
                      textAnchor="middle" dominantBaseline="middle"
                      fontSize={Math.min(node.r * 0.42, 11)}
                      fill="white" fillOpacity={0.85}
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                      {h.label || h.address_short}
                    </text>
                  )}
                  {/* % label below for medium bubbles */}
                  {node.r >= 10 && (
                    <text
                      x={node.x} y={node.y + node.r * 0.45}
                      textAnchor="middle" dominantBaseline="middle"
                      fontSize={Math.min(node.r * 0.3, 9)}
                      fill="white" fillOpacity={0.55}
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                      {h.pct.toFixed(1)}%
                    </text>
                  )}
                </g>
              )
            })}

            {/* Tooltip */}
            {tooltip && (() => {
              const h   = tooltip.node.holder
              const col = h.is_lp ? LP_COLOUR : clusterColour(h.cluster_id)
              const tx  = Math.min(tooltip.mx + 12, W - 160)
              const ty  = Math.max(tooltip.my - 50, 6)
              const cluster = data?.clusters.find(c => c.id === h.cluster_id)
              return (
                <g>
                  <rect x={tx} y={ty} width={155} height={cluster ? 70 : 52}
                    rx={6} fill="#18181b" stroke={col} strokeOpacity={0.5} strokeWidth={1} />
                  <text x={tx + 8} y={ty + 16} fontSize={10} fill="white" fillOpacity={0.9}
                    fontWeight="bold">{h.label || h.address_short}</text>
                  <text x={tx + 8} y={ty + 30} fontSize={9} fill="white" fillOpacity={0.6}>
                    {h.pct.toFixed(2)}% of supply
                  </text>
                  {cluster && (
                    <>
                      <text x={tx + 8} y={ty + 44} fontSize={9} fill={col} fillOpacity={0.9}>
                        Cluster with {cluster.wallet_count}w · {cluster.combined_pct}% total
                      </text>
                      <text x={tx + 8} y={ty + 58} fontSize={9} fill={col} fillOpacity={0.7}>
                        Funder: {cluster.master_short}
                      </text>
                    </>
                  )}
                </g>
              )
            })()}
          </svg>
        </div>
      )}

      {/* Cluster legend */}
      {!loading && data && data.clusters.length > 0 && (
        <div className="flex flex-wrap gap-3 pt-1">
          {data.clusters.map(c => (
            <div key={c.id} className="flex items-center gap-1.5 text-[10px]">
              <div className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                style={{ background: CLUSTER_COLOURS[c.id % CLUSTER_COLOURS.length] }} />
              <span className="text-zinc-300">
                Cluster {c.id + 1}: {c.wallet_count} wallets · {c.combined_pct}% supply
              </span>
              <span
                className="px-1.5 py-0.5 rounded-full font-bold"
                style={{
                  background: RISK_COLOURS[c.risk] + '22',
                  color: RISK_COLOURS[c.risk],
                  fontSize: 9,
                }}
              >{c.risk}</span>
            </div>
          ))}
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && data && data.holders.length === 0 && (
        <div className="flex items-center justify-center h-[100px] text-zinc-600 text-xs">
          No holder data available
        </div>
      )}

      {/* Skip reason */}
      {!loading && data?.skip_reason && (
        <p className="text-[10px] text-zinc-600 italic">
          Cluster trace: {data.skip_reason === 'token_too_old'
            ? 'token too old — funding trace unavailable'
            : data.skip_reason}
        </p>
      )}
    </GlassCard>
  )
}

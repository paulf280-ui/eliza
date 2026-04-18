import { GlassCard } from '../common/GlassCard'

// ── Mini SVG sparkline drawn from price_history pnl_pct values ───────────────
function PnlSparkline({ history, slPct }: { history: PricePoint[], slPct: number }) {
  if (history.length < 2) {
    return (
      <div className="flex items-center justify-center h-full text-zinc-700 text-[9px]">
        building chart…
      </div>
    )
  }
  const W = 220, H = 56
  const values = history.map(p => p.pnl_pct)
  const minV = Math.min(-slPct * 0.5, Math.min(...values))
  const maxV = Math.max(slPct * 0.5, Math.max(...values), 5)
  const range = maxV - minV || 1
  const toX = (i: number) => (i / (values.length - 1)) * W
  const toY = (v: number) => H - ((v - minV) / range) * H
  const zeroY = toY(0)

  const points = values.map((v, i) => `${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(' ')
  const areaPath = `M${toX(0)},${zeroY} ` +
    values.map((v, i) => `L${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(' ') +
    ` L${W},${zeroY} Z`

  const lastV = values[values.length - 1]
  const isGreen = lastV >= 0
  const lineColor = isGreen ? '#34d399' : '#f87171'
  const areaColor = isGreen ? 'rgba(52,211,153,0.12)' : 'rgba(248,113,113,0.12)'

  // SL line
  const slY = toY(-slPct)
  // Zero line
  const slLabelY = Math.min(slY + 10, H - 2)

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="overflow-visible">
      {/* zero line */}
      <line x1={0} y1={zeroY} x2={W} y2={zeroY}
        stroke="rgba(255,255,255,0.08)" strokeWidth={1} strokeDasharray="3,3" />
      {/* SL line */}
      <line x1={0} y1={slY} x2={W} y2={slY}
        stroke="rgba(248,113,113,0.4)" strokeWidth={1} strokeDasharray="2,4" />
      <text x={2} y={slLabelY} fill="rgba(248,113,113,0.6)" fontSize={7}>
        SL -{slPct}%
      </text>
      {/* filled area */}
      <path d={areaPath} fill={areaColor} />
      {/* line */}
      <polyline points={points} fill="none" stroke={lineColor} strokeWidth={1.5}
        strokeLinejoin="round" strokeLinecap="round" />
      {/* current dot */}
      <circle cx={toX(values.length - 1)} cy={toY(lastV)} r={2.5}
        fill={lineColor} />
    </svg>
  )
}

// ── PnL progress bar: shows position between SL and a rough upside target ────
function PnlBar({ pnl, peak, slPct }: { pnl: number, peak: number, slPct: number }) {
  // Range: -slPct to +max(peak*1.2, slPct)
  const lo = -slPct
  const hi = Math.max(peak * 1.2, slPct, 20)
  const range = hi - lo
  const toFrac = (v: number) => Math.max(0, Math.min(1, (v - lo) / range))

  const zeroFrac   = toFrac(0)
  const currFrac   = toFrac(pnl)
  const peakFrac   = toFrac(peak)
  const isGreen    = pnl >= 0

  return (
    <div className="relative w-full h-3 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.05)' }}>
      {/* red zone: lo → zero */}
      <div className="absolute top-0 bottom-0 left-0 rounded-l-full"
        style={{ width: `${zeroFrac * 100}%`, background: 'rgba(248,113,113,0.18)' }} />
      {/* green zone fill from zero to current (if positive) */}
      {isGreen && (
        <div className="absolute top-0 bottom-0"
          style={{
            left: `${zeroFrac * 100}%`,
            width: `${(currFrac - zeroFrac) * 100}%`,
            background: 'rgba(52,211,153,0.35)',
          }} />
      )}
      {/* red fill from current to zero (if negative) */}
      {!isGreen && (
        <div className="absolute top-0 bottom-0"
          style={{
            left: `${currFrac * 100}%`,
            width: `${(zeroFrac - currFrac) * 100}%`,
            background: 'rgba(248,113,113,0.4)',
          }} />
      )}
      {/* peak marker */}
      {peak > 0 && (
        <div className="absolute top-0 bottom-0 w-0.5 rounded"
          style={{ left: `${peakFrac * 100}%`, background: '#fbbf24', opacity: 0.8 }} />
      )}
      {/* SL boundary */}
      <div className="absolute top-0 bottom-0 w-px" style={{ left: '0%', background: 'rgba(248,113,113,0.6)' }} />
      {/* zero line */}
      <div className="absolute top-0 bottom-0 w-px"
        style={{ left: `${zeroFrac * 100}%`, background: 'rgba(255,255,255,0.25)' }} />
      {/* current cursor */}
      <div className="absolute top-0 bottom-0 w-1 rounded"
        style={{
          left: `calc(${currFrac * 100}% - 2px)`,
          background: isGreen ? '#34d399' : '#f87171',
          boxShadow: isGreen ? '0 0 4px #34d399' : '0 0 4px #f87171',
        }} />
    </div>
  )
}

interface PricePoint {
  ts: number
  pnl_pct: number
}

interface FrostOpenPosition {
  mint: string
  token_name: string
  mode: 'main' | 'lotto'
  sol_spent: number
  whale_sol: number
  entry_price: number
  current_price: number
  pnl_pct: number
  peak_pnl_pct: number
  entry_ts: number
  hold_mins: number
  sl_pct: number
  price_history?: PricePoint[]
}

interface FrostClosedTrade {
  ts: number
  sell_ts: number
  mint: string
  token_name: string
  mode: 'main' | 'lotto'
  sol_spent: number
  whale_sol: number
  entry_price: number
  exit_price: number
  pnl_pct: number
  pnl_sol: number
  peak_pnl_pct: number
  hold_mins: number
  reason: string
}

export interface FrostMirrorStats {
  total: number
  wins: number
  losses: number
  win_rate: number
  net_pnl: number
  unrealised_pnl: number
  open_count: number
  main_count: number
  lotto_count: number
  main_net: number
  lotto_net: number
  closed_trades: FrostClosedTrade[]
  open_positions: FrostOpenPosition[]
}

interface Props {
  stats: FrostMirrorStats | null
  focusMode?: boolean
}

function relTime(ts: number): string {
  const diff = Math.floor((Date.now() / 1000) - ts)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function reasonLabel(r: string): string {
  if (r.startsWith('wallet_exit')) return '🐋 Frost exit'
  if (r.startsWith('stop_loss'))  return '🛑 SL'
  if (r === 'manual_close')       return '✋ manual'
  return r.replace(/_/g, ' ')
}

function ModeTag({ mode }: { mode: string }) {
  const isLotto = mode === 'lotto'
  return (
    <span
      className="text-[9px] px-1.5 py-0.5 rounded font-bold"
      style={isLotto
        ? { background: 'rgba(167,139,250,0.15)', color: '#a78bfa' }
        : { background: 'rgba(251,146,60,0.15)',  color: '#fb923c' }}>
      {isLotto ? '🎰 lotto' : '🎯 main'}
    </span>
  )
}

export default function FrostMirrorPanel({ stats, focusMode = false }: Props) {
  // Null-safe: in focus mode we always render the panel (shows waiting state)
  const s = stats ?? {
    total: 0, wins: 0, losses: 0, win_rate: 0, net_pnl: 0, unrealised_pnl: 0,
    open_count: 0, main_count: 0, lotto_count: 0, main_net: 0, lotto_net: 0,
    closed_trades: [], open_positions: [],
  }
  const hasData = s.total > 0 || s.open_count > 0

  const cardStyle = focusMode
    ? { border: '1px solid rgba(14,165,233,0.25)', background: 'rgba(6,20,40,0.7)' }
    : undefined

  return (
    <GlassCard
      className={`flex flex-col min-h-0 ${focusMode ? 'min-h-[520px]' : 'h-full'}`}
      style={cardStyle}
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-4 flex-shrink-0">
        <div className="flex items-center gap-3">
          {focusMode ? (
            <span className="text-sm font-bold tracking-wide" style={{ color: '#67e8f9' }}>
              ❄ Frost Mirror
            </span>
          ) : (
            <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">
              ❄️ Frost Mirror
            </span>
          )}
          {s.total > 0 && (
            <span className={`text-xs font-mono font-semibold ${s.win_rate >= 50 ? 'text-emerald-400' : 'text-amber-400'}`}>
              {s.win_rate.toFixed(0)}% WR ({s.wins}W / {s.losses}L)
            </span>
          )}
          {s.open_count > 0 && (
            <span className="text-[10px] font-mono animate-pulse" style={{ color: '#38bdf8' }}>
              {s.open_count} open
            </span>
          )}
        </div>
        <div className="flex items-center gap-4">
          {/* Per-mode breakdown */}
          {(s.main_count > 0 || s.lotto_count > 0) && (
            <div className="flex gap-3 text-[11px] font-mono">
              <span style={{ color: '#fb923c' }}>
                🎯 main {s.main_count}T: {s.main_net >= 0 ? '+' : ''}{s.main_net.toFixed(4)}
              </span>
              <span style={{ color: '#a78bfa' }}>
                🎰 lotto {s.lotto_count}T: {s.lotto_net >= 0 ? '+' : ''}{s.lotto_net.toFixed(4)}
              </span>
            </div>
          )}
          {hasData && (
            <div className="flex flex-col items-end">
              <span className={`${focusMode ? 'text-base' : 'text-sm'} font-bold font-mono ${s.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {s.net_pnl >= 0 ? '+' : ''}{s.net_pnl.toFixed(4)} SOL
              </span>
              {s.unrealised_pnl !== 0 && (
                <span className="text-[10px] font-mono text-zinc-500">
                  {s.unrealised_pnl >= 0 ? '+' : ''}{s.unrealised_pnl.toFixed(4)} unrealised
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {!hasData ? (
        <div className="flex-1 flex flex-col items-center justify-center gap-3 text-zinc-600">
          <div className="text-4xl">❄️</div>
          <div className="text-sm font-medium" style={{ color: '#94a3b8' }}>Frost Mirror active — waiting for first signal</div>
          <div className="flex flex-col items-center gap-1 text-xs text-zinc-700">
            <div>Mirrors every Frost buy automatically</div>
            <div style={{ color: '#fb923c' }}>🎯 Main: 0.15 SOL · SL -25% · Frost buy ≥0.5 SOL</div>
            <div style={{ color: '#a78bfa' }}>🎰 Lotto: 0.08 SOL · SL -60% · Frost buy &lt;0.5 SOL</div>
            <div className="mt-2 text-zinc-700">Exit: wallet mirror only · No TP · Lotto rides for moonshot</div>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-4 flex-1 min-h-0">

          {/* Open positions */}
          {s.open_positions.length > 0 && (
            <div className="flex-shrink-0">
              <div className="text-[10px] uppercase tracking-widest mb-2" style={{ color: '#38bdf8' }}>
                Open Positions
              </div>
              <div className="flex flex-col gap-3">
                {s.open_positions.map((p, i) => {
                  const isGreen = p.pnl_pct >= 0
                  const distToSl = p.sl_pct + p.pnl_pct  // how far above SL trigger we are
                  const holdStr = p.hold_mins < 60
                    ? `${p.hold_mins.toFixed(0)}m`
                    : `${(p.hold_mins / 60).toFixed(1)}h`
                  const history = p.price_history ?? []
                  return (
                    <div key={`${p.mint}-${i}`}
                      className="rounded-xl p-3 flex flex-col gap-2"
                      style={{
                        background: isGreen ? 'rgba(52,211,153,0.04)' : 'rgba(239,68,68,0.04)',
                        border: `1px solid ${isGreen ? 'rgba(52,211,153,0.18)' : 'rgba(239,68,68,0.18)'}`,
                      }}>

                      {/* ── Top row: name / mode / P&L ── */}
                      <div className="flex items-center gap-2">
                        <ModeTag mode={p.mode} />
                        <a href={`https://dexscreener.com/solana/${p.mint}`}
                          target="_blank" rel="noreferrer"
                          className="font-bold hover:text-white transition-colors truncate text-sm"
                          style={{ color: '#e2e8f0', maxWidth: focusMode ? 220 : 140 }}>
                          {p.token_name}
                        </a>
                        <span className={`font-mono font-black text-base ml-auto ${isGreen ? 'text-emerald-400' : 'text-red-400'}`}>
                          {p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct.toFixed(2)}%
                        </span>
                        {p.peak_pnl_pct > 0.1 && (
                          <span className="font-mono text-amber-400 text-xs whitespace-nowrap">
                            ⬆ {p.peak_pnl_pct.toFixed(1)}%
                          </span>
                        )}
                      </div>

                      {/* ── Progress bar ── */}
                      <PnlBar pnl={p.pnl_pct} peak={p.peak_pnl_pct} slPct={p.sl_pct} />

                      {/* ── Bar labels ── */}
                      <div className="flex items-center justify-between text-[9px] font-mono -mt-1">
                        <span className="text-red-500">SL -{p.sl_pct.toFixed(0)}%</span>
                        <span className="text-zinc-600">0%</span>
                        {p.peak_pnl_pct > 0.1 && (
                          <span className="text-amber-500">peak +{p.peak_pnl_pct.toFixed(1)}%</span>
                        )}
                      </div>

                      {/* ── Sparkline ── */}
                      <div className="rounded-lg overflow-hidden" style={{ background: 'rgba(0,0,0,0.25)', height: 60 }}>
                        <PnlSparkline history={history} slPct={p.sl_pct} />
                      </div>

                      {/* ── Bottom stats row ── */}
                      <div className="flex items-center gap-4 text-[10px] font-mono text-zinc-500 mt-0.5">
                        <span>⏱ {holdStr}</span>
                        <span>🐋 {p.whale_sol.toFixed(3)} SOL</span>
                        <span>in {p.sol_spent.toFixed(2)} SOL</span>
                        <span className={distToSl < 5 ? 'text-red-400 font-bold' : 'text-zinc-600'}>
                          {distToSl.toFixed(1)}% above SL
                        </span>
                        <a href={`https://dexscreener.com/solana/${p.mint}`}
                          target="_blank" rel="noreferrer"
                          className="ml-auto text-sky-600 hover:text-sky-400 transition-colors">
                          dex ↗
                        </a>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Closed trades */}
          {s.closed_trades.length > 0 && (
            <div className="flex-1 min-h-0 overflow-y-auto">
              <div className="text-[10px] text-zinc-600 uppercase tracking-widest mb-1.5">Closed Trades</div>
              <table className="w-full text-xs">
                <thead className="sticky top-0" style={{ background: 'rgba(6,8,13,0.97)' }}>
                  <tr className="text-zinc-500 border-b border-zinc-800">
                    <th className="text-left pb-2 font-medium">Time</th>
                    <th className="text-left pb-2 font-medium">Token</th>
                    <th className="text-left pb-2 font-medium">Mode</th>
                    <th className="text-right pb-2 font-medium">P&L</th>
                    <th className="text-right pb-2 font-medium">Peak</th>
                    <th className="text-right pb-2 font-medium">Whale</th>
                    <th className="text-right pb-2 font-medium">Hold</th>
                    <th className="text-left pb-2 font-medium">Exit</th>
                  </tr>
                </thead>
                <tbody>
                  {s.closed_trades.map((t, i) => {
                    const win = t.pnl_pct >= 0
                    const moon = t.pnl_pct >= 500
                    const big = t.pnl_pct >= 100
                    const isLotto = t.mode === 'lotto'
                    return (
                      <tr key={`${t.mint}-${i}`}
                        className="border-b border-zinc-800/40 hover:bg-zinc-800/20 transition-colors"
                        style={moon
                          ? { background: 'rgba(167,139,250,0.08)' }
                          : big ? { background: 'rgba(52,211,153,0.04)' }
                          : undefined}>
                        <td className="py-1.5 text-zinc-500 font-mono whitespace-nowrap">{relTime(t.sell_ts)}</td>
                        <td className="py-1.5">
                          <a href={`https://dexscreener.com/solana/${t.mint}`} target="_blank" rel="noreferrer"
                            className={`hover:opacity-80 font-semibold transition-colors ${isLotto ? 'text-violet-300' : 'text-orange-300'}`}>
                            {moon ? '🚀 ' : big ? '⭐ ' : ''}
                            {t.token_name.length > 16 ? t.token_name.slice(0, 16) + '…' : t.token_name}
                          </a>
                        </td>
                        <td className="py-1.5">
                          <ModeTag mode={t.mode} />
                        </td>
                        <td className={`py-1.5 text-right font-mono font-bold ${win ? 'text-emerald-400' : 'text-red-400'}`}>
                          {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct.toFixed(1)}%
                          <div className={`text-[9px] font-normal ${win ? 'text-emerald-600' : 'text-red-600'}`}>
                            {t.pnl_sol >= 0 ? '+' : ''}{t.pnl_sol.toFixed(4)} SOL
                          </div>
                        </td>
                        <td className="py-1.5 text-right font-mono text-amber-500 text-[10px]">
                          {t.peak_pnl_pct > 0 ? `+${t.peak_pnl_pct.toFixed(1)}%` : '—'}
                        </td>
                        <td className="py-1.5 text-right font-mono text-zinc-500 text-[10px]">
                          {t.whale_sol.toFixed(2)}
                        </td>
                        <td className="py-1.5 text-right font-mono text-zinc-500">
                          {t.hold_mins < 60 ? `${t.hold_mins.toFixed(0)}m` : `${(t.hold_mins / 60).toFixed(1)}h`}
                        </td>
                        <td className="py-1.5 text-zinc-500 whitespace-nowrap">{reasonLabel(t.reason)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </GlassCard>
  )
}

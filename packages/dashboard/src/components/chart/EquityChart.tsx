import { useEffect, useRef } from 'react'
import { useDashboardStore } from '../../store/dashboard'
import { GlassCard } from '../common/GlassCard'

export default function EquityChart() {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<any>(null)
  const seriesRef = useRef<any>(null)
  const { equityHistory, wallet } = useDashboardStore()

  useEffect(() => {
    if (!containerRef.current) return

    // Dynamic import to avoid SSR issues
    import('lightweight-charts').then(({ createChart, ColorType, LineStyle }) => {
      if (!containerRef.current || chartRef.current) return

      chartRef.current = createChart(containerRef.current, {
        layout: {
          background: { type: ColorType.Solid, color: 'transparent' },
          textColor: '#71717a',
          fontFamily: 'JetBrains Mono, monospace',
        },
        grid: {
          vertLines: { color: 'rgba(63,63,70,0.3)', style: LineStyle.Dotted },
          horzLines: { color: 'rgba(63,63,70,0.3)', style: LineStyle.Dotted },
        },
        crosshair: {
          vertLine: { color: 'rgba(16,185,129,0.5)', labelBackgroundColor: '#10b981' },
          horzLine: { color: 'rgba(16,185,129,0.5)', labelBackgroundColor: '#10b981' },
        },
        rightPriceScale: {
          borderColor: 'rgba(63,63,70,0.3)',
          textColor: '#71717a',
        },
        timeScale: {
          borderColor: 'rgba(63,63,70,0.3)',
          timeVisible: true,
          secondsVisible: false,
          tickMarkFormatter: (time: number) => {
            const d = new Date(time * 1000)
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          },
        },
        width: containerRef.current.clientWidth,
        height: 180,
        handleScroll: true,
        handleScale: true,
      })

      seriesRef.current = chartRef.current.addAreaSeries({
        lineColor: '#10b981',
        topColor: 'rgba(16,185,129,0.25)',
        bottomColor: 'rgba(16,185,129,0.01)',
        lineWidth: 2,
        priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
        lastValueVisible: true,
        priceLineVisible: true,
        priceLineColor: 'rgba(16,185,129,0.4)',
      })

      // Responsive resize
      const ro = new ResizeObserver(() => {
        if (containerRef.current && chartRef.current) {
          chartRef.current.applyOptions({ width: containerRef.current.clientWidth })
        }
      })
      ro.observe(containerRef.current)
      return () => ro.disconnect()
    })

    return () => {
      if (chartRef.current) {
        chartRef.current.remove()
        chartRef.current = null
        seriesRef.current = null
      }
    }
  }, [])

  // Update series data when equity history changes
  useEffect(() => {
    if (!seriesRef.current) return
    const points = equityHistory.map(p => ({
      time: Math.floor(p.timestamp) as any,
      value: p.equity_sol,
    }))
    if (points.length > 0) {
      seriesRef.current.setData(points)
      chartRef.current?.timeScale().fitContent()
    }
  }, [equityHistory])

  const totalReturn = equityHistory.length > 1
    ? ((equityHistory[equityHistory.length - 1].equity_sol - equityHistory[0].equity_sol) / equityHistory[0].equity_sol) * 100
    : 0

  return (
    <GlassCard className="overflow-hidden">
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-semibold text-zinc-400 uppercase tracking-widest">Equity Curve</span>
        <div className="flex items-center gap-4 text-xs">
          {wallet && (
            <span className="text-zinc-400 font-mono">
              {wallet.sol_balance.toFixed(4)} SOL
            </span>
          )}
          {equityHistory.length > 1 && (
            <span className={`font-mono font-semibold ${totalReturn >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {totalReturn >= 0 ? '+' : ''}{totalReturn.toFixed(2)}%
            </span>
          )}
        </div>
      </div>
      {equityHistory.length === 0 ? (
        <div className="h-44 flex items-center justify-center text-zinc-700 text-sm">
          Equity data will appear after the first price snapshot (every 5 min)
        </div>
      ) : (
        <div ref={containerRef} className="w-full" />
      )}
    </GlassCard>
  )
}

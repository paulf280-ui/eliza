import { useState, useEffect, useRef, useCallback } from 'react'

export interface LivePriceData {
  price: number | null        // SOL-denominated price (highest liq pair)
  pnlPct: number | null       // (price/entry - 1) * 100
  pnlSol: number | null       // solSpent * pnlPct/100
  mcUsd: number | null        // current MC in USD
  buyRatio: number | null     // h1 buy ratio (0-100)
  liqUsd: number | null       // current liquidity USD
  ageSecs: number             // seconds since last successful fetch
  fetching: boolean
}

const EMPTY: LivePriceData = {
  price: null, pnlPct: null, pnlSol: null,
  mcUsd: null, buyRatio: null, liqUsd: null,
  ageSecs: 0, fetching: false,
}

// PRICE/P&L comes from our backend /api/live-price, which reads the POOL RESERVES
// via Helius (same source as GMGN and the bot's TP engine) — so it moves at chart
// speed, not DexScreener's 10-30s lag. DexScreener is still polled in parallel for
// slow-changing context (MC, liquidity, buy ratio).
export function useLivePrice(
  mint: string | null,
  entryPrice: number,
  solSpent: number,
  intervalMs = 1500,
): LivePriceData {
  const [data, setData] = useState<LivePriceData>(EMPTY)
  const lastFetchTs = useRef<number>(Date.now())
  const mountedRef = useRef(true)

  const fetchPrice = useCallback(async () => {
    if (!mint || entryPrice <= 0) return
    setData(d => ({ ...d, fetching: true }))
    try {
      const [liveRes, dexRes] = await Promise.allSettled([
        fetch(`/api/live-price?mint=${mint}`, { signal: AbortSignal.timeout(3000) }),
        fetch(`https://api.dexscreener.com/latest/dex/tokens/${mint}`, { signal: AbortSignal.timeout(4000) }),
      ])
      if (!mountedRef.current) return

      // real-time pool price (preferred for P&L)
      let livePrice: number | null = null
      if (liveRes.status === 'fulfilled' && liveRes.value.ok) {
        const lj = await liveRes.value.json()
        if (lj?.price_sol && lj.price_sol > 0) livePrice = lj.price_sol
      }

      // DexScreener context (MC/liq/buyRatio) + price fallback
      let dexPrice: number | null = null, mcUsd: number | null = null
      let liqUsd: number | null = null, buyRatio: number | null = null
      if (dexRes.status === 'fulfilled' && dexRes.value.ok) {
        const json = await dexRes.value.json()
        const pairs: any[] = (json.pairs || []).sort(
          (a: any, b: any) => parseFloat(b?.liquidity?.usd ?? 0) - parseFloat(a?.liquidity?.usd ?? 0))
        const p0 = pairs[0]
        if (p0) {
          dexPrice = parseFloat(p0.priceNative ?? 0) || null
          mcUsd = parseFloat(p0.marketCap ?? p0.fdv ?? 0) || null
          liqUsd = parseFloat(p0.liquidity?.usd ?? 0) || null
          const txH1 = p0.txns?.h1 ?? {}
          const buys = parseInt(txH1.buys ?? 0), sells = parseInt(txH1.sells ?? 0)
          buyRatio = (buys + sells) > 0 ? (buys / (buys + sells)) * 100 : null
        }
      }
      if (!mountedRef.current) return

      const price = livePrice ?? dexPrice          // real-time pool price wins
      const pnlPct = price && entryPrice > 0 ? ((price / entryPrice) - 1) * 100 : null
      const pnlSol = pnlPct !== null ? solSpent * (pnlPct / 100) : null

      lastFetchTs.current = Date.now()
      setData({ price, pnlPct, pnlSol, mcUsd, buyRatio, liqUsd, ageSecs: 0, fetching: false })
    } catch {
      if (mountedRef.current) setData(d => ({ ...d, fetching: false }))
    }
  }, [mint, entryPrice, solSpent])

  // Age counter — ticks every second so the staleness indicator updates
  useEffect(() => {
    const id = setInterval(() => {
      if (mountedRef.current)
        setData(d => ({ ...d, ageSecs: Math.floor((Date.now() - lastFetchTs.current) / 1000) }))
    }, 1000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    mountedRef.current = true
    if (!mint) return
    fetchPrice()
    const id = setInterval(fetchPrice, intervalMs)
    return () => {
      mountedRef.current = false
      clearInterval(id)
    }
  }, [mint, fetchPrice, intervalMs])

  return data
}

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

// Polls DexScreener directly from the browser — bypasses the backend
// so P&L is always current, not dependent on the 6s monitor tick.
export function useLivePrice(
  mint: string | null,
  entryPrice: number,
  solSpent: number,
  intervalMs = 3000,
): LivePriceData {
  const [data, setData] = useState<LivePriceData>(EMPTY)
  const lastFetchTs = useRef<number>(Date.now())
  const mountedRef = useRef(true)

  const fetchPrice = useCallback(async () => {
    if (!mint || entryPrice <= 0) return
    setData(d => ({ ...d, fetching: true }))
    try {
      const res = await fetch(
        `https://api.dexscreener.com/latest/dex/tokens/${mint}`,
        { signal: AbortSignal.timeout(4000) }
      )
      if (!res.ok || !mountedRef.current) return
      const json = await res.json()
      const pairs: any[] = (json.pairs || []).sort(
        (a: any, b: any) =>
          parseFloat(b?.liquidity?.usd ?? 0) - parseFloat(a?.liquidity?.usd ?? 0)
      )
      const p0 = pairs[0]
      if (!p0 || !mountedRef.current) return

      const price = parseFloat(p0.priceNative ?? 0) || null
      const mcUsd = parseFloat(p0.marketCap ?? p0.fdv ?? 0) || null
      const liqUsd = parseFloat(p0.liquidity?.usd ?? 0) || null
      const txH1 = p0.txns?.h1 ?? {}
      const buys = parseInt(txH1.buys ?? 0)
      const sells = parseInt(txH1.sells ?? 0)
      const buyRatio = (buys + sells) > 0 ? (buys / (buys + sells)) * 100 : null

      const pnlPct = price && entryPrice > 0
        ? ((price / entryPrice) - 1) * 100
        : null
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

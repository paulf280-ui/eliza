import { useEffect, useRef, useCallback } from 'react'
import { useDashboardStore } from '../store/dashboard'
import type { WsEvent } from '../types'

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectDelay = useRef(1000)
  const store = useDashboardStore

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    const ws = new WebSocket(`${protocol}//${host}/ws`)
    wsRef.current = ws

    ws.onopen = () => {
      store.getState().setConnected(true)
      reconnectDelay.current = 1000
    }

    ws.onclose = () => {
      store.getState().setConnected(false)
      const delay = reconnectDelay.current
      reconnectDelay.current = Math.min(delay * 2, 30000)
      setTimeout(connect, delay)
    }

    ws.onerror = () => ws.close()

    ws.onmessage = (event) => {
      try {
        const msg: WsEvent = JSON.parse(event.data)
        const state = store.getState()

        // Dispatch to chat listeners
        window.dispatchEvent(new CustomEvent('ws_event', { detail: msg }))

        switch (msg.type) {
          case 'init':
            if (msg.data) {
              state.updateWallet(msg.data.wallet)
              state.updateRisk(msg.data.risk)
              Object.values(msg.data.positions ?? {}).forEach((p: any) => state.addPosition(p))
            }
            break
          case 'position_update':
            state.updatePosition(msg.data)
            break
          case 'position_opened':
            state.addPosition(msg.data)
            state.addActivity({
              timestamp: Date.now() / 1000,
              level: 'success',
              message: `Position opened: ${(msg.data.mint ?? '').slice(0, 8)}…`,
            })
            break
          case 'position_closed':
            state.removePosition(msg.data.mint)
            state.addTrade(msg.data)
            state.addActivity({
              timestamp: Date.now() / 1000,
              level: (msg.data.pnl_sol ?? 0) >= 0 ? 'success' : 'warning',
              message: `Closed ${(msg.data.mint ?? '').slice(0, 8)}… ${msg.data.reason} P&L: ${(msg.data.pnl_sol ?? 0).toFixed(4)} SOL`,
            })
            break
          case 'partial_sell':
            state.addActivity({
              timestamp: Date.now() / 1000,
              level: 'success',
              message: `${msg.data.level} hit: ${(msg.data.mint ?? '').slice(0, 8)}… sold at ${msg.data.exit_price_sol?.toExponential(4)}`,
            })
            break
          case 'wallet_update':
            state.updateWallet(msg.data)
            break
          case 'risk_update':
            state.updateRisk(msg.data)
            break
          case 'equity_snapshot':
            state.addEquityPoint(msg.data)
            break
          case 'new_launch':
            state.addLaunch(msg.data)
            break
          case 'activity':
            state.addActivity(msg.data)
            break
          case 'config_update':
            if (msg.data) state.updateConfig(msg.data)
            break
          case 'heartbeat':
            break
          case 'chat_response':
            break
        }
      } catch {
        // Ignore malformed messages
      }
    }
  }, [])

  const sendCommand = useCallback((action: string, payload?: Record<string, unknown>) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action, ...payload }))
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
      }
    }
  }, [connect])

  return { sendCommand }
}

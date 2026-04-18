import { create } from 'zustand'
import type {
  WalletData,
  PositionData,
  RiskData,
  TradeRecord,
  EquityPoint,
  ActivityEntry,
  LaunchEntry,
  BotConfig,
  StatusResponse,
} from '../types'

interface DashboardState {
  connected: boolean
  wallet: WalletData | null
  positions: Record<string, PositionData>
  risk: RiskData | null
  tradeHistory: TradeRecord[]
  equityHistory: EquityPoint[]
  activityLog: ActivityEntry[]
  recentLaunches: LaunchEntry[]
  config: BotConfig | null
  lastUpdate: number
  selectedMint: string | null

  setConnected: (connected: boolean) => void
  setSelectedMint: (mint: string | null) => void
  setInitialState: (data: StatusResponse) => void
  updatePosition: (data: PositionData) => void
  addPosition: (data: PositionData) => void
  removePosition: (mint: string) => void
  addTrade: (trade: TradeRecord) => void
  updateWallet: (wallet: Partial<WalletData>) => void
  updateRisk: (risk: RiskData) => void
  addEquityPoint: (point: EquityPoint) => void
  addActivity: (entry: ActivityEntry) => void
  addLaunch: (launch: LaunchEntry) => void
  updateConfig: (config: BotConfig) => void
}

export const useDashboardStore = create<DashboardState>((set) => ({
  connected: false,
  wallet: null,
  positions: {},
  risk: null,
  tradeHistory: [],
  equityHistory: [],
  activityLog: [],
  recentLaunches: [],
  config: null,
  lastUpdate: 0,
  selectedMint: null,

  setConnected: (connected) => set({ connected }),
  setSelectedMint: (mint) => set({ selectedMint: mint }),

  setInitialState: (data) =>
    set({
      wallet: data.wallet,
      positions: data.positions,
      risk: data.risk,
      tradeHistory: data.trade_history,
      equityHistory: data.equity_history,
      activityLog: data.activity_log,
      recentLaunches: data.recent_launches,
      config: data.config,
      lastUpdate: Date.now(),
    }),

  updatePosition: (data) =>
    set((state) => ({
      positions: { ...state.positions, [data.mint]: data },
      lastUpdate: Date.now(),
    })),

  addPosition: (data) =>
    set((state) => ({
      positions: { ...state.positions, [data.mint]: data },
      lastUpdate: Date.now(),
    })),

  removePosition: (mint) =>
    set((state) => {
      const { [mint]: _, ...rest } = state.positions
      return { positions: rest, lastUpdate: Date.now() }
    }),

  addTrade: (trade) =>
    set((state) => ({
      tradeHistory: [trade, ...state.tradeHistory].slice(0, 200),
      lastUpdate: Date.now(),
    })),

  updateWallet: (wallet) =>
    set((state) => ({
      wallet: state.wallet ? { ...state.wallet, ...wallet } : null,
      lastUpdate: Date.now(),
    })),

  updateRisk: (risk) => set({ risk, lastUpdate: Date.now() }),

  addEquityPoint: (point) =>
    set((state) => ({
      equityHistory: [...state.equityHistory, point],
      lastUpdate: Date.now(),
    })),

  addActivity: (entry) =>
    set((state) => ({
      activityLog: [entry, ...state.activityLog].slice(0, 200),
      lastUpdate: Date.now(),
    })),

  addLaunch: (launch) =>
    set((state) => ({
      recentLaunches: [launch, ...state.recentLaunches].slice(0, 50),
      lastUpdate: Date.now(),
    })),

  updateConfig: (config) => set({ config, lastUpdate: Date.now() }),
}))

export interface WalletData {
  address: string
  sol_balance: number
  token_balances: TokenBalance[]
  read_only: boolean
}

export interface TokenBalance {
  mint: string
  amount: number
  decimals: number
}

export interface PositionData {
  mint: string
  dex: string
  entry_price_sol: number
  entry_sol_spent: number
  token_amount: number
  token_decimals: number
  stop_loss_price: number
  tp1_price: number
  tp2_price: number
  tp3_price: number
  peak_price: number
  trailing_stop_price: number
  tp1_hit: boolean
  tp2_hit: boolean
  tp3_hit: boolean
  entry_time: number
  age_seconds: number
  current_price_sol: number
  pnl_pct: number
  unrealized_pnl_sol: number
}

export interface RiskData {
  daily_pnl_sol: number
  consecutive_losses: number
  circuit_broken: boolean
  circuit_broken_secs?: number
  day_start: number
  open_position_count: number
  max_daily_loss_pct: number
  max_consecutive_losses: number
}

export interface TradeRecord {
  id: string
  mint: string
  dex: string
  side: 'buy' | 'sell'
  entry_price_sol: number
  exit_price_sol: number | null
  sol_amount: number
  pnl_pct: number | null
  pnl_sol: number | null
  reason: string
  timestamp: number
  signature: string
}

export interface EquityPoint {
  timestamp: number
  equity_sol: number
  sol_balance?: number
  unrealized_sol?: number
  realized_pnl_sol?: number
  position_count?: number
}

export interface ActivityEntry {
  timestamp: number
  level: 'info' | 'success' | 'warning' | 'error'
  message: string
}

export interface LaunchEntry {
  mint: string
  signature: string
  timestamp: number
  price_sol: number
  progress_pct: number
  complete?: boolean
}

export interface BotConfig {
  max_position_pct: number
  min_position_sol: number
  max_concurrent_positions: number
  stop_loss_pct: number
  tp1_mult: number
  tp1_fraction: number
  tp2_mult: number
  tp2_fraction: number
  tp3_mult: number
  tp3_fraction: number
  trailing_stop_pct: number
  stall_time_hours: number
  stall_threshold_pct: number
  max_daily_loss_pct: number
  max_consecutive_losses: number
  max_hold_pump_fun_hours: number
  max_hold_raydium_hours: number
  monitor_interval_secs: number
  paper_trading?: boolean
  monster_max_concurrent?: number
  monster_default_size_sol?: number
}

export interface StatusResponse {
  timestamp: number
  wallet: WalletData
  positions: Record<string, PositionData>
  risk: RiskData
  trade_history: TradeRecord[]
  equity_history: EquityPoint[]
  activity_log: ActivityEntry[]
  recent_launches: LaunchEntry[]
  config: BotConfig
  paper_trading: boolean
}

export interface WsEvent {
  type: string
  data: any
}

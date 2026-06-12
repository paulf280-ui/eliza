/** Shared types for the Cabal-Hunter MCP Server */

export interface Cluster {
  master_wallet: string
  master_short: string
  wallet_count: number
  combined_pct: number
  risk: "HIGH" | "MEDIUM"
  /** "funding" = shared funding source; "time_sync" = same-block (bundled) buys */
  type?: "funding" | "time_sync"
}

export interface DeployerReport {
  creator: string | null
  creator_short: string | null
  tokens_launched: number
  dead: number
  sampled: number
  dead_pct: number
  verdict: "FIRST_LAUNCH" | "SERIAL_RUGGER" | "POOR_TRACK_RECORD" | "NORMAL" | "UNKNOWN"
}

export interface Holder {
  rank: number
  address: string
  address_short: string
  pct: number
  cluster_id: number | null
  is_lp: boolean
  label: string | null
}

export interface CabalReport {
  mint: string
  token_name: string
  risk: "HIGH" | "MEDIUM" | "CLEAN"
  cabal_score: number           // 0–100
  is_controlled: boolean        // true if score >= 35
  verdict: string               // human-readable summary sentence
  coordinated_clusters: Cluster[]
  holders: Holder[]
  /** true if ≥3 top holders bought in the exact same block (bundle signature) */
  time_sync: boolean
  /** deployer wallet track record — null when creator can't be resolved */
  deployer: DeployerReport | null
  wallets_checked: number
  analysis_time_ms: number
  source: "pre_indexed" | "real_time"
  cached_age_seconds?: number
  pair_created_ts?: number
  computed_at?: number
}

export interface PaymentRequest {
  recipient: string
  amount_usdc: number
  usdc_mint: string
  memo_required: string          // nonce to include in transaction memo
  expires_at_unix: number
  instructions: string
}

export interface PaymentVerification {
  valid: boolean
  tx_signature?: string
  amount_usdc?: number
  error?: string
}

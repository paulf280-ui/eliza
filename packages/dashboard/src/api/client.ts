const API_BASE = ''

export async function fetchStatus() {
  const res = await fetch(`${API_BASE}/api/status`)
  if (!res.ok) throw new Error(`Status API error: ${res.status}`)
  return res.json()
}

export async function fetchPositions() {
  const res = await fetch(`${API_BASE}/api/positions`)
  if (!res.ok) throw new Error(`Positions API error: ${res.status}`)
  return res.json()
}

export async function fetchHistory(limit = 50, offset = 0) {
  const res = await fetch(`${API_BASE}/api/history?limit=${limit}&offset=${offset}`)
  if (!res.ok) throw new Error(`History API error: ${res.status}`)
  return res.json()
}

export async function fetchEquity(since = 0) {
  const res = await fetch(`${API_BASE}/api/equity?since=${since}`)
  if (!res.ok) throw new Error(`Equity API error: ${res.status}`)
  return res.json()
}

export async function fetchLaunches(limit = 20) {
  const res = await fetch(`${API_BASE}/api/launches?limit=${limit}`)
  if (!res.ok) throw new Error(`Launches API error: ${res.status}`)
  return res.json()
}

export async function sendMessage(text: string, userId?: string, roomId?: string) {
  const res = await fetch(`${API_BASE}/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, user_id: userId, room_id: roomId }),
  })
  if (!res.ok) throw new Error(`Message API error: ${res.status}`)
  return res.json()
}

export async function fetchControl(action: 'pause' | 'resume' | 'reset_cb') {
  const res = await fetch(`${API_BASE}/api/control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  })
  if (!res.ok) throw new Error(`Control API error: ${res.status}`)
  return res.json()
}

export async function closePosition(mint: string) {
  const res = await fetch(`${API_BASE}/api/positions/${encodeURIComponent(mint)}/close`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  })
  if (!res.ok) throw new Error(`Close position API error: ${res.status}`)
  return res.json()
}

export async function fetchReport() {
  const res = await fetch(`${API_BASE}/api/report`)
  if (!res.ok) throw new Error(`Report API error: ${res.status}`)
  return res.json()
}

export interface LifecycleRejectsResponse {
  window_hours: number
  cycles: number
  candidates_total: number
  entered_total: number
  viral_fires_total?: number
  totals: Record<string, number>
  per_cycle_avg: Record<string, number>
  recent: Array<{
    ts: number
    candidates: number
    fresh_grads: number
    in_age: number
    baseline: number
    watchlisted: number
    deferred_total: number
    entered: number
    viral_fires?: number
    rejects: Record<string, number>
  }>
}

export async function fetchLifecycleRejects(): Promise<LifecycleRejectsResponse> {
  const res = await fetch(`${API_BASE}/api/lifecycle/rejects`)
  if (!res.ok) throw new Error(`Lifecycle rejects API error: ${res.status}`)
  return res.json()
}

export interface CreatorAlphaResponse {
  recent: Array<{
    kind: 'direct_create' | 'operator_fund' | 'operator_create' | 'graduated'
    ts?: number
    detected_ts?: number
    creator?: string
    parent?: string
    parent_op?: string
    child?: string
    mint?: string
    amount_sol?: number
    source?: string
    lag_secs?: number
  }>
  operators: Array<{
    wallet: string
    last_fund_ts: number
    fund_count: number
    create_count: number
    graduated_count: number
  }>
  watched_children: Array<{
    child: string
    parent: string
    amount_sol: number
    funded_ts: number
    ttl_remaining_secs: number
  }>
  pending_mints: Array<{
    mint: string
    source: string
    creator: string
    parent_op: string | null
    detected_ts: number
    age_secs: number
    ttl_remaining_secs: number
  }>
  tracked_direct: number
  tracked_operators: number
  watched_count: number
  pending_count: number
  signal_count: number
}

export async function fetchCreatorAlpha(): Promise<CreatorAlphaResponse> {
  const res = await fetch(`${API_BASE}/api/creator-alpha`)
  if (!res.ok) throw new Error(`Creator-alpha API error: ${res.status}`)
  return res.json()
}

export interface PerformanceBySourceResponse {
  sources: Array<{
    source: string
    trades: number
    wins: number
    losses: number
    flat: number
    win_rate_pct: number
    avg_pnl_pct: number
    total_pnl_sol: number
    best_pct: number
    worst_pct: number
    avg_peak_pct: number
    recent_trades: Array<{ token: string; pnl_pct: number; peak_pct: number; ts_close: number }>
  }>
  totals: {
    trades: number
    wins: number
    total_pnl_sol: number
    window_hours: number
    overall_wr_pct: number
  }
}

export async function fetchPerformanceBySource(hours = 24): Promise<PerformanceBySourceResponse> {
  const res = await fetch(`${API_BASE}/api/performance-by-source?hours=${hours}`)
  if (!res.ok) throw new Error(`Performance API error: ${res.status}`)
  return res.json()
}

export async function patchConfig(updates: Record<string, unknown>) {
  const res = await fetch(`${API_BASE}/api/config`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) throw new Error(`Patch config error: ${res.status}`)
  return res.json()
}

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

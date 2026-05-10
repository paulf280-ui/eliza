import { useState, useRef, useEffect, useCallback } from 'react'
import { GlassCard } from '../common/GlassCard'
import { useDashboardStore } from '../../store/dashboard'

// ── Types ──────────────────────────────────────────────────────────────────────

interface ToolCall {
  name: string
  cmd: string
  result?: string
}

interface Message {
  role: 'user' | 'bot'
  content: string
  timestamp: number
  toolCalls?: ToolCall[]
  streaming?: boolean
}

// ── Icons ──────────────────────────────────────────────────────────────────────

const MicIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-4 h-4">
    <path d="M12 1a4 4 0 0 1 4 4v6a4 4 0 0 1-8 0V5a4 4 0 0 1 4-4Z" />
    <path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8" strokeLinecap="round" strokeLinejoin="round" fill="none" stroke="currentColor" strokeWidth="1.5" />
  </svg>
)

const SpeakerIcon = ({ muted }: { muted: boolean }) => (
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" className="w-4 h-4">
    <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor" stroke="none" opacity="0.8" />
    {muted ? (
      <><line x1="23" y1="9" x2="17" y2="15" /><line x1="17" y1="9" x2="23" y2="15" /></>
    ) : (
      <><path d="M15.54 8.46a5 5 0 0 1 0 7.07" /><path d="M19.07 4.93a10 10 0 0 1 0 14.14" /></>
    )}
  </svg>
)

// ── Constants ──────────────────────────────────────────────────────────────────

const QUICK_COMMANDS = [
  { label: 'Sitrep',    cmd: 'Give me a full sitrep — wallet, open positions, recent trades, scanner state.' },
  { label: 'Logs',      cmd: 'Show me the last 30 lines of the bot logs, filtering out HTTP noise.' },
  { label: 'Positions', cmd: 'What positions are open right now? Show entry price, PnL, age.' },
  { label: 'P&L',       cmd: 'Break down P&L for today and the last 7 days.' },
  { label: 'Config',    cmd: 'Show me the current live config — trade size, max concurrent, strategy flags.' },
  { label: 'Scanner',   cmd: 'What is the scanner seeing? Show last few lifecycle cycles and reject reasons.' },
  { label: 'Brains',    cmd: 'Show Groq brain status — last fired, decisions, session stats.' },
  { label: 'Deploy',    cmd: 'What files have changed locally vs what is deployed on AWS? How do I deploy?' },
]

const WAKE_WORDS = ['hey jarvis', 'jarvis', 'hey travis']

// ── Component ──────────────────────────────────────────────────────────────────

interface BotChatProps {
  sendCommand: (action: string, payload?: Record<string, unknown>) => void
}

declare global {
  interface Window {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    SpeechRecognition: any
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    webkitSpeechRecognition: any
  }
}

export default function BotChat({ sendCommand: _sendCommand }: BotChatProps) {
  const [messages, setMessages] = useState<Message[]>([{
    role: 'bot',
    content: "J.A.R.V.I.S. online. I have full access to the trading server — bash, files, configs, logs. Ask me anything or give me a task.",
    timestamp: Date.now() / 1000,
  }])
  const [input, setInput]             = useState('')
  const [sending, setSending]         = useState(false)
  const [listening, setListening]     = useState(false)
  const [wakeActive, setWakeActive]   = useState(false)
  const [voiceEnabled, setVoiceEnabled] = useState(true)
  const [ttsPlaying, setTtsPlaying]   = useState(false)
  const [wakeWordReady, setWakeWordReady] = useState(false)
  const [interimText, setInterimText] = useState('')

  const { connected } = useDashboardStore()
  const listRef          = useRef<HTMLDivElement>(null)
  const lastBotMsgRef    = useRef<HTMLDivElement>(null)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const commandRecRef    = useRef<any>(null)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const wakeRecRef       = useRef<any>(null)
  const audioRef         = useRef<HTMLAudioElement | null>(null)
  const wakeRestartTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const transitioningRef = useRef(false)
  const abortRef         = useRef<AbortController | null>(null)

  // Auto-scroll to latest bot message
  useEffect(() => {
    const t = setTimeout(() => {
      if (lastBotMsgRef.current) {
        lastBotMsgRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
      } else if (listRef.current) {
        listRef.current.scrollTop = listRef.current.scrollHeight
      }
    }, 80)
    return () => clearTimeout(t)
  }, [messages.length])

  // ── Wake word listener ───────────────────────────────────────────────────────

  useEffect(() => {
    const API = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!API) return
    setWakeWordReady(true)
    startWakeListener(API)
    return () => {
      if (wakeRecRef.current) wakeRecRef.current.stop()
      if (wakeRestartTimer.current) clearTimeout(wakeRestartTimer.current)
    }
  }, [])

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const startWakeListener = useCallback((API: any) => {
    if (wakeRecRef.current) { try { wakeRecRef.current.stop() } catch { /**/ } }
    const rec = new API()
    rec.lang = 'en-GB'; rec.continuous = true; rec.interimResults = true; rec.maxAlternatives = 1
    wakeRecRef.current = rec
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    rec.onresult = (e: any) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript.trim().toLowerCase()
        if (WAKE_WORDS.some(w => t.includes(w))) {
          transitioningRef.current = true; rec.stop(); setWakeActive(true); playWakeTone()
          setTimeout(() => { transitioningRef.current = false; startCommandListener(API) }, 700)
          break
        }
      }
    }
    rec.onend = () => {
      if (!listening && !transitioningRef.current)
        wakeRestartTimer.current = setTimeout(() => { const A = window.SpeechRecognition || window.webkitSpeechRecognition; if (A && !transitioningRef.current) startWakeListener(A) }, 500)
    }
    rec.onerror = () => {
      if (!transitioningRef.current)
        wakeRestartTimer.current = setTimeout(() => { const A = window.SpeechRecognition || window.webkitSpeechRecognition; if (A) startWakeListener(A) }, 2000)
    }
    try { rec.start() } catch { /**/ }
  }, [listening])

  const playWakeTone = useCallback(() => {
    try {
      const ctx = new AudioContext(); const osc = ctx.createOscillator(); const gain = ctx.createGain()
      osc.connect(gain); gain.connect(ctx.destination)
      osc.frequency.setValueAtTime(880, ctx.currentTime); osc.frequency.linearRampToValueAtTime(1200, ctx.currentTime + 0.12)
      gain.gain.setValueAtTime(0.15, ctx.currentTime); gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18)
      osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.18)
    } catch { /**/ }
  }, [])

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const startCommandListener = useCallback((API: any) => {
    if (commandRecRef.current) { try { commandRecRef.current.stop() } catch { /**/ } }
    const rec = new API()
    rec.lang = 'en-GB'; rec.continuous = false; rec.interimResults = true; rec.maxAlternatives = 1
    commandRecRef.current = rec; setListening(true); setInterimText('')
    const silenceTimer = setTimeout(() => { try { rec.stop() } catch { /**/ } }, 8000)
    const cleanup = () => {
      clearTimeout(silenceTimer); setListening(false); setWakeActive(false); setInterimText('')
      const A = window.SpeechRecognition || window.webkitSpeechRecognition
      if (A) setTimeout(() => startWakeListener(A), 500)
    }
    rec.onend = cleanup; rec.onerror = () => { clearTimeout(silenceTimer); cleanup() }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    rec.onresult = (e: any) => {
      let interim = '', final = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript
        if (e.results[i].isFinal) final += t; else interim += t
      }
      if (interim) setInterimText(interim)
      if (final.trim()) {
        clearTimeout(silenceTimer); setInterimText('')
        let cmd = final.trim()
        for (const w of WAKE_WORDS) cmd = cmd.replace(new RegExp(`^${w}[,\\s]+`, 'i'), '')
        if (cmd.trim()) send(cmd.trim())
      }
    }
    try { rec.start() } catch { /**/ }
  }, [startWakeListener])

  const toggleListening = useCallback(() => {
    const API = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!API) { alert('Voice input requires Chrome or Edge.'); return }
    if (listening) { commandRecRef.current?.stop(); setListening(false); return }
    playWakeTone(); startCommandListener(API)
  }, [listening, playWakeTone, startCommandListener])

  // ── TTS ─────────────────────────────────────────────────────────────────────

  // Ref to signal stop to the TTS playback loop
  const ttsStopRef = useRef(false)

  // Split text into speakable chunks — sentences and line-breaks
  const splitForSpeech = useCallback((text: string): string[] => {
    return text
      // strip markdown code fences, inline code, tool output markers
      .replace(/```[\s\S]*?```/g, 'code block omitted.')
      .replace(/`[^`]+`/g, '')
      .replace(/\[.*?\]\(.*?\)/g, '')   // markdown links
      .replace(/#+\s/g, '')             // headings
      // split on sentence endings followed by whitespace+capital, or on newlines
      .split(/(?<=[.!?])\s+(?=[A-Z"'])|(?<=\n)\s*(?=[A-Z•\-\d])|\n{2,}/)
      .map(s => s.replace(/[•\-*]\s*/g, '').trim())
      .filter(s => s.length > 3)
  }, [])

  const speakAsJarvis = useCallback(async (text: string) => {
    ttsStopRef.current = false
    const chunks = splitForSpeech(text)
    if (chunks.length === 0) return

    setTtsPlaying(true)

    // Pre-fetch all chunks in parallel — they'll be ready by the time we need them
    const fetches = chunks.map(chunk =>
      fetch('/api/speak', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: chunk }),
      })
        .then(r => r.ok ? r.blob() : null)
        .catch(() => null)
    )

    for (const blobPromise of fetches) {
      if (ttsStopRef.current) break
      const blob = await blobPromise
      if (!blob || ttsStopRef.current) continue

      const url = URL.createObjectURL(blob)
      await new Promise<void>(resolve => {
        const audio = new Audio(url)
        audioRef.current = audio
        audio.onended  = () => { URL.revokeObjectURL(url); resolve() }
        audio.onerror  = () => { URL.revokeObjectURL(url); resolve() }
        audio.play().catch(() => resolve())
      })
    }

    setTtsPlaying(false)
    audioRef.current = null
  }, [splitForSpeech])

  const stopTts = useCallback(() => {
    ttsStopRef.current = true
    if (audioRef.current) { audioRef.current.pause() }
    setTtsPlaying(false)
  }, [])

  // ── Core send — streams from /api/jarvis ─────────────────────────────────────

  const send = useCallback(async (text: string) => {
    if (!text.trim() || sending) return
    if (abortRef.current) abortRef.current.abort()
    abortRef.current = new AbortController()

    // Append user message
    const userMsg: Message = { role: 'user', content: text, timestamp: Date.now() / 1000 }
    setMessages(prev => [...prev, userMsg])
    setInput('')
    setSending(true)

    // Prepare bot placeholder (streaming)
    const botIdx = Date.now()
    const botPlaceholder: Message = { role: 'bot', content: '', timestamp: botIdx / 1000, toolCalls: [], streaming: true }
    setMessages(prev => [...prev, botPlaceholder])

    // Build history from current messages (text only, last 12)
    const history = messages.slice(-12).map(m => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content,
    }))

    try {
      const res = await fetch('/api/jarvis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history }),
        signal: abortRef.current.signal,
      })
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

      const reader  = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer    = ''
      let finalText = ''

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // Parse complete SSE lines
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''     // keep incomplete line

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          let event: Record<string, string>
          try { event = JSON.parse(line.slice(6)) } catch { continue }

          if (event.type === 'text') {
            finalText += event.content
            setMessages(prev => prev.map(m =>
              m.timestamp === botIdx / 1000
                ? { ...m, content: finalText }
                : m
            ))
          } else if (event.type === 'tool_call') {
            setMessages(prev => prev.map(m =>
              m.timestamp === botIdx / 1000
                ? { ...m, toolCalls: [...(m.toolCalls ?? []), { name: event.name, cmd: event.cmd ?? '' }] }
                : m
            ))
          } else if (event.type === 'tool_result') {
            setMessages(prev => prev.map(m => {
              if (m.timestamp !== botIdx / 1000) return m
              const calls = [...(m.toolCalls ?? [])]
              // update last call with the same name
              const idx = calls.map(c => c.name).lastIndexOf(event.name)
              if (idx >= 0) calls[idx] = { ...calls[idx], result: event.preview }
              return { ...m, toolCalls: calls }
            }))
          } else if (event.type === 'error') {
            finalText += `\n[Error: ${event.message}]`
            setMessages(prev => prev.map(m =>
              m.timestamp === botIdx / 1000 ? { ...m, content: finalText } : m
            ))
          } else if (event.type === 'done') {
            break
          }
        }
      }

      // Mark streaming complete
      setMessages(prev => prev.map(m =>
        m.timestamp === botIdx / 1000 ? { ...m, streaming: false } : m
      ))
      if (voiceEnabled && finalText) speakAsJarvis(finalText)

    } catch (err: unknown) {
      if ((err as Error)?.name === 'AbortError') {
        // User hit Stop — keep whatever partial content exists
        setMessages(prev => prev.map(m =>
          m.timestamp === botIdx / 1000 ? { ...m, content: m.content || '(cancelled)', streaming: false } : m
        ))
      } else {
        // Network error — keep any content Jarvis produced (e.g. bot restarted mid-stream)
        setMessages(prev => prev.map(m => {
          if (m.timestamp !== botIdx / 1000) return m
          const note = m.content
            ? '\n\n[Connection dropped — the bot may have restarted]'
            : `Error: ${String(err)}`
          return { ...m, content: m.content + note, streaming: false }
        }))
      }
    } finally {
      setSending(false)
    }
  }, [messages, sending, voiceEnabled, speakAsJarvis])

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <GlassCard className="flex flex-col h-full min-h-0">
      {/* Header */}
      <div className="flex items-center justify-between mb-3 flex-shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-bold tracking-[0.2em] text-cyan-400/90 font-mono">J.A.R.V.I.S.</span>
          <div className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-500 animate-pulse' : 'bg-red-500'}`} />
          {wakeWordReady && (
            <span className="text-[9px] text-zinc-600 tracking-wider">
              {wakeActive || listening
                ? <span className="text-cyan-400/80 animate-pulse">● LISTENING</span>
                : <span>say "Hey Jarvis"</span>}
            </span>
          )}
          <span className="text-[9px] text-purple-400/60 ml-1">Gemini 2.5 Flash</span>
        </div>
        <div className="flex items-center gap-1.5">
          {sending && (
            <button
              onClick={() => abortRef.current?.abort()}
              className="px-2 py-1 rounded text-xs bg-red-500/20 text-red-400 border border-red-500/30 hover:bg-red-500/30 transition-all"
            >
              Stop
            </button>
          )}
          {ttsPlaying && (
            <button onClick={stopTts} className="px-2 py-1 rounded text-xs bg-amber-500/20 text-amber-400 border border-amber-500/30 hover:bg-amber-500/30 transition-all">
              Mute
            </button>
          )}
          <button
            onClick={() => setVoiceEnabled(v => !v)}
            title={voiceEnabled ? 'Voice ON' : 'Voice OFF'}
            className={`p-1.5 rounded transition-all border ${voiceEnabled ? 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30' : 'bg-zinc-800 text-zinc-500 border-zinc-700'}`}
          >
            <SpeakerIcon muted={!voiceEnabled} />
          </button>
        </div>
      </div>

      {/* Quick commands */}
      <div className="flex flex-wrap gap-1 mb-2 flex-shrink-0">
        {QUICK_COMMANDS.map(({ label, cmd }) => (
          <button key={label} onClick={() => send(cmd)} disabled={sending}
            className="px-2 py-0.5 rounded text-xs bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200 border border-zinc-700 transition-all disabled:opacity-40">
            {label}
          </button>
        ))}
      </div>

      {/* Message list */}
      <div ref={listRef} className="flex-1 overflow-y-auto space-y-2 mb-2 custom-scrollbar min-h-0">
        {messages.map((msg, i) => {
          const isLastBot = msg.role === 'bot' && i === messages.map(m => m.role).lastIndexOf('bot')
          return (
            <div key={i} className="w-full" ref={isLastBot ? lastBotMsgRef : undefined}>
              {msg.role === 'user' ? (
                <div className="w-full rounded-lg px-3 py-2 text-xs leading-relaxed bg-emerald-500/20 text-emerald-200 border border-emerald-500/20">
                  {msg.content}
                </div>
              ) : (
                <div className="w-full rounded-lg border border-zinc-700/50 overflow-hidden">
                  {/* Tool calls */}
                  {(msg.toolCalls ?? []).map((tc, ti) => (
                    <div key={ti} className="px-3 py-1.5 border-b border-zinc-700/30 bg-zinc-900/60">
                      <div className="flex items-center gap-1.5 text-[9px]">
                        <span className="text-amber-400/80 font-mono font-bold">⚡ {tc.name}</span>
                        {tc.cmd && <span className="text-zinc-500 font-mono truncate max-w-[200px]">{tc.cmd}</span>}
                      </div>
                      {tc.result && (
                        <pre className="text-[9px] text-zinc-500 font-mono mt-0.5 whitespace-pre-wrap line-clamp-3">{tc.result}</pre>
                      )}
                    </div>
                  ))}

                  {/* Bot text */}
                  <div className="px-3 py-2 text-xs leading-relaxed bg-zinc-800/80 text-zinc-300 whitespace-pre-wrap">
                    <span className="text-cyan-500/70 text-[10px] font-mono tracking-widest mr-1.5">J.A.R.V.I.S.</span>
                    {msg.content}
                    {msg.streaming && (
                      <span className="inline-flex gap-0.5 ml-1 align-middle">
                        <span className="w-1 h-1 bg-cyan-500/60 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                        <span className="w-1 h-1 bg-cyan-500/60 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                        <span className="w-1 h-1 bg-cyan-500/60 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                      </span>
                    )}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Input row */}
      <div className="flex gap-2 flex-shrink-0">
        <button onClick={toggleListening} disabled={sending}
          className={`px-3 py-2 rounded-lg border text-xs transition-all disabled:opacity-40 ${listening || wakeActive ? 'bg-cyan-500/30 text-cyan-300 border-cyan-400/50 animate-pulse' : 'bg-zinc-800/60 text-zinc-400 border-zinc-700 hover:bg-zinc-700'}`}>
          <MicIcon />
        </button>
        <input
          type="text" value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send(input)}
          placeholder={listening || wakeActive ? '● Listening…' : 'Ask Jarvis anything — or give a task…'}
          disabled={sending || listening || wakeActive}
          className="flex-1 bg-zinc-800/60 border border-zinc-700 rounded-lg px-3 py-2 text-xs text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-cyan-500/50 disabled:opacity-40 transition-colors"
        />
        <button onClick={() => send(input)} disabled={sending || !input.trim()}
          className="px-3 py-2 rounded-lg bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30 border border-cyan-500/30 text-xs transition-all disabled:opacity-40">
          Send
        </button>
      </div>

      {(listening || wakeActive) && (
        <div className="mt-1.5 text-center text-[10px] text-cyan-400/60 animate-pulse flex-shrink-0">
          {wakeActive && !listening ? 'Wake word detected — speak your command…'
            : interimText ? <span className="text-zinc-200">"{interimText}"</span>
            : 'Listening…'}
        </div>
      )}
    </GlassCard>
  )
}

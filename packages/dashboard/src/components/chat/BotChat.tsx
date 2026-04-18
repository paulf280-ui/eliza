import { useState, useRef, useEffect, useCallback } from 'react'
import { GlassCard } from '../common/GlassCard'
import { useDashboardStore } from '../../store/dashboard'

interface Message {
  role: 'user' | 'bot'
  text: string
  timestamp: number
}

const QUICK_COMMANDS = [
  { label: 'Sitrep',    cmd: 'Sitrep' },
  { label: 'Positions', cmd: 'Positions' },
  { label: 'Brains',    cmd: 'Brain status' },
  { label: 'Scan',      cmd: 'Scanner report' },
  { label: 'P&L',       cmd: 'P&L breakdown' },
  { label: 'Learning',  cmd: 'What have you learned?' },
  { label: 'Risk',      cmd: 'Risk check' },
  { label: 'Alpha',     cmd: "Where's the alpha?" },
]

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

const API_BASE = ''

// Wake words — any of these trigger active listening
const WAKE_WORDS = ['hey jarvis', 'jarvis', 'hey travis', 'hey travis']

export default function BotChat({ sendCommand }: BotChatProps) {
  const [messages, setMessages] = useState<Message[]>([{
    role: 'bot',
    text: "Good morning. J.A.R.V.I.S. online. All systems nominal. I have full command over the bot. How can I assist you, sir?",
    timestamp: Date.now() / 1000,
  }])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [listening, setListening] = useState(false)
  const [wakeActive, setWakeActive] = useState(false)   // continuous background wake-word listener
  const [voiceEnabled, setVoiceEnabled] = useState(true)
  const [ttsPlaying, setTtsPlaying] = useState(false)
  const [wakeWordReady, setWakeWordReady] = useState(false)
  const [interimText, setInterimText] = useState('')     // live transcript while speaking
  const { connected } = useDashboardStore()
  const listRef = useRef<HTMLDivElement>(null)
  const lastBotMsgRef = useRef<HTMLDivElement>(null)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const commandRecRef = useRef<any>(null)   // active command listener
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const wakeRecRef = useRef<any>(null)      // continuous wake-word listener
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const wakeRestartTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const transitioningRef = useRef(false)    // true during wake→command handoff, blocks wake restart

  useEffect(() => {
    // Scroll to the start of the latest bot message so the user reads from the top.
    // Use a short delay so the DOM has finished rendering the full message.
    const timer = setTimeout(() => {
      if (lastBotMsgRef.current) {
        lastBotMsgRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
      } else if (listRef.current) {
        listRef.current.scrollTop = listRef.current.scrollHeight
      }
    }, 80)
    return () => clearTimeout(timer)
  }, [messages.length])

  // ── Boot wake-word listener once ────────────────────────────────────────────
  useEffect(() => {
    const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SpeechRecognitionAPI) return
    setWakeWordReady(true)
    startWakeListener(SpeechRecognitionAPI)
    return () => {
      if (wakeRecRef.current) wakeRecRef.current.stop()
      if (wakeRestartTimerRef.current) clearTimeout(wakeRestartTimerRef.current)
    }
  }, [])

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const startWakeListener = useCallback((SpeechRecognitionAPI: any) => {
    if (wakeRecRef.current) {
      try { wakeRecRef.current.stop() } catch { /* ignore */ }
    }
    const rec = new SpeechRecognitionAPI()
    rec.lang = 'en-GB'
    rec.continuous = true
    rec.interimResults = true
    rec.maxAlternatives = 1
    wakeRecRef.current = rec

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    rec.onresult = (event: any) => {
      // Look through all results for a wake word
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript.trim().toLowerCase()
        const triggered = WAKE_WORDS.some(w => transcript.includes(w))
        if (triggered) {
          transitioningRef.current = true   // block wake listener from restarting
          rec.stop()
          setWakeActive(true)
          // Brief audible ping — play a short tone
          playWakeTone()
          // Start active command listener after 700ms — gives user time to hear the tone
          // and begin speaking. Clears transitioningRef once command listener is running.
          setTimeout(() => {
            transitioningRef.current = false
            startCommandListener(SpeechRecognitionAPI)
          }, 700)
          break
        }
      }
    }

    rec.onend = () => {
      // Auto-restart wake listener — but NOT if we're transitioning to command mode
      if (!listening && !transitioningRef.current) {
        wakeRestartTimerRef.current = setTimeout(() => {
          const API = window.SpeechRecognition || window.webkitSpeechRecognition
          if (API && !transitioningRef.current) startWakeListener(API)
        }, 500)
      }
    }

    rec.onerror = () => {
      // Restart silently on error — but not during command handoff
      if (!transitioningRef.current) {
        wakeRestartTimerRef.current = setTimeout(() => {
          const API = window.SpeechRecognition || window.webkitSpeechRecognition
          if (API) startWakeListener(API)
        }, 2000)
      }
    }

    try { rec.start() } catch { /* browser may reject if already running */ }
  }, [listening])

  const playWakeTone = useCallback(() => {
    try {
      const ctx = new AudioContext()
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.connect(gain)
      gain.connect(ctx.destination)
      osc.frequency.setValueAtTime(880, ctx.currentTime)
      osc.frequency.linearRampToValueAtTime(1200, ctx.currentTime + 0.12)
      gain.gain.setValueAtTime(0.15, ctx.currentTime)
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18)
      osc.start(ctx.currentTime)
      osc.stop(ctx.currentTime + 0.18)
    } catch { /* no AudioContext available */ }
  }, [])

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const startCommandListener = useCallback((SpeechRecognitionAPI: any) => {
    if (commandRecRef.current) {
      try { commandRecRef.current.stop() } catch { /* ignore */ }
    }
    const rec = new SpeechRecognitionAPI()
    rec.lang = 'en-GB'
    rec.continuous = false
    rec.interimResults = true   // show live transcript so user knows it's working
    rec.maxAlternatives = 1
    commandRecRef.current = rec

    setListening(true)
    setInterimText('')

    // 8-second silence fallback — if browser never fires onresult, reset cleanly
    const silenceTimer = setTimeout(() => {
      try { rec.stop() } catch { /* ignore */ }
    }, 8000)

    const cleanup = () => {
      clearTimeout(silenceTimer)
      setListening(false)
      setWakeActive(false)
      setInterimText('')
      const API = window.SpeechRecognition || window.webkitSpeechRecognition
      if (API) setTimeout(() => startWakeListener(API), 500)
    }

    rec.onend = cleanup

    rec.onerror = () => {
      clearTimeout(silenceTimer)
      cleanup()
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    rec.onresult = (event: any) => {
      let interim = ''
      let finalTranscript = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const t = event.results[i][0].transcript
        if (event.results[i].isFinal) finalTranscript += t
        else interim += t
      }
      // Show live interim text in input area
      if (interim) setInterimText(interim)
      if (finalTranscript.trim()) {
        clearTimeout(silenceTimer)
        setInterimText('')
        // Strip wake word if user said "Hey Jarvis, show me the wallet" in one breath
        let cmd = finalTranscript.trim()
        for (const w of WAKE_WORDS) {
          const wRe = new RegExp(`^${w}[,\\s]+`, 'i')
          cmd = cmd.replace(wRe, '')
        }
        if (cmd.trim()) send(cmd.trim())
      }
    }

    try { rec.start() } catch { /* ignore */ }
  }, [startWakeListener])

  // Manual mic button — also starts command listener
  const toggleListening = useCallback(() => {
    const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SpeechRecognitionAPI) {
      alert('Voice input requires Chrome or Edge.')
      return
    }
    if (listening) {
      commandRecRef.current?.stop()
      setListening(false)
      return
    }
    playWakeTone()
    startCommandListener(SpeechRecognitionAPI)
  }, [listening, playWakeTone, startCommandListener])

  // Listen for chat responses from WebSocket
  useEffect(() => {
    const handler = (e: CustomEvent) => {
      const { type, data } = e.detail
      if (type === 'chat_response') {
        const text = data.text || '(no response)'
        setMessages(prev => [...prev, {
          role: 'bot',
          text,
          timestamp: data.timestamp,
        }])
        setSending(false)
        if (voiceEnabled) speakAsJarvis(text)
      }
    }
    window.addEventListener('ws_event' as any, handler)
    return () => window.removeEventListener('ws_event' as any, handler)
  }, [voiceEnabled])

  const speakAsJarvis = useCallback(async (text: string) => {
    try {
      const trimmed = text.length > 400
        ? text.slice(0, 380).replace(/\s+\S*$/, '') + '…'
        : text
      const res = await fetch(`${API_BASE}/api/speak`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: trimmed }),
      })
      if (!res.ok) return
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      if (audioRef.current) {
        audioRef.current.pause()
        URL.revokeObjectURL(audioRef.current.src)
      }
      const audio = new Audio(url)
      audioRef.current = audio
      setTtsPlaying(true)
      audio.onended = () => { setTtsPlaying(false); URL.revokeObjectURL(url) }
      audio.onerror = () => setTtsPlaying(false)
      await audio.play()
    } catch { setTtsPlaying(false) }
  }, [])

  const send = async (text: string) => {
    if (!text.trim() || !connected || sending) return
    setMessages(prev => [...prev, { role: 'user', text, timestamp: Date.now() / 1000 }])
    setInput('')
    setSending(true)
    sendCommand('send_message', { text })
    setTimeout(() => setSending(false), 120000)
  }

  const stopTts = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause()
      setTtsPlaying(false)
    }
  }, [])

  return (
    <GlassCard className="flex flex-col h-full min-h-0">
      {/* Header */}
      <div className="flex items-center justify-between mb-3 flex-shrink-0">
        <div className="flex items-center gap-2">
          {/* J.A.R.V.I.S. logo */}
          <span className="text-[11px] font-bold tracking-[0.2em] text-cyan-400/90 font-mono">
            J.A.R.V.I.S.
          </span>
          <div className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-500 animate-pulse' : 'bg-red-500'}`} />
          {wakeWordReady && (
            <span className="text-[9px] text-zinc-600 tracking-wider">
              {wakeActive || listening ? (
                <span className="text-cyan-400/80 animate-pulse">● LISTENING</span>
              ) : (
                <span className="text-zinc-600">say "Hey Jarvis"</span>
              )}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {ttsPlaying && (
            <button onClick={stopTts} title="Stop speaking"
              className="px-2 py-1 rounded text-xs bg-amber-500/20 text-amber-400 border border-amber-500/30 hover:bg-amber-500/30 transition-all">
              Stop
            </button>
          )}
          <button
            onClick={() => setVoiceEnabled(v => !v)}
            title={voiceEnabled ? 'Jarvis voice ON' : 'Jarvis voice OFF'}
            className={`p-1.5 rounded transition-all border ${
              voiceEnabled
                ? 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30 hover:bg-cyan-500/30'
                : 'bg-zinc-800 text-zinc-500 border-zinc-700 hover:bg-zinc-700'
            }`}
          >
            <SpeakerIcon muted={!voiceEnabled} />
          </button>
        </div>
      </div>

      {/* Quick commands */}
      <div className="flex flex-wrap gap-1 mb-2 flex-shrink-0">
        {QUICK_COMMANDS.map(({ label, cmd }) => (
          <button key={label} onClick={() => send(cmd)}
            disabled={!connected || sending}
            className="px-2 py-0.5 rounded text-xs bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200 border border-zinc-700 transition-all disabled:opacity-40 disabled:cursor-not-allowed">
            {label}
          </button>
        ))}
      </div>

      {/* Message list */}
      <div ref={listRef} className="flex-1 overflow-y-auto space-y-2 mb-2 custom-scrollbar">
        {messages.map((msg, i) => {
          const isLastBot = msg.role === 'bot' && i === messages.map(m => m.role).lastIndexOf('bot')
          return (
            <div key={i} className="w-full" ref={isLastBot ? lastBotMsgRef : undefined}>
              <div className={`w-full rounded-lg px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap ${
                msg.role === 'user'
                  ? 'bg-emerald-500/20 text-emerald-200 border border-emerald-500/20'
                  : 'bg-zinc-800/80 text-zinc-300 border border-zinc-700/50'
              }`}>
                {msg.role === 'bot' && (
                  <span className="text-cyan-500/70 text-[10px] font-mono tracking-widest mr-1.5">J.A.R.V.I.S.</span>
                )}
                {msg.text}
              </div>
            </div>
          )
        })}
        {sending && (
          <div className="flex justify-start">
            <div className="bg-zinc-800/80 text-zinc-500 text-xs px-3 py-2 rounded-lg border border-zinc-700/50">
              <span className="text-cyan-500/70 text-[10px] font-mono tracking-widest mr-1.5">J.A.R.V.I.S.</span>
              <span className="inline-flex gap-1 ml-1">
                <span className="w-1 h-1 bg-cyan-500/60 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                <span className="w-1 h-1 bg-cyan-500/60 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                <span className="w-1 h-1 bg-cyan-500/60 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
              </span>
            </div>
          </div>
        )}
      </div>

      {/* Input row */}
      <div className="flex gap-2 flex-shrink-0">
        <button
          onClick={toggleListening}
          title={listening ? 'Stop' : 'Click to speak, or say "Hey Jarvis"'}
          className={`px-3 py-2 rounded-lg border text-xs transition-all ${
            listening || wakeActive
              ? 'bg-cyan-500/30 text-cyan-300 border-cyan-400/50 animate-pulse'
              : 'bg-zinc-800/60 text-zinc-400 border-zinc-700 hover:bg-zinc-700 hover:text-zinc-200'
          } disabled:opacity-40`}
          disabled={!connected || sending}
        >
          <MicIcon />
        </button>

        <input
          type="text"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send(input)}
          placeholder={listening || wakeActive ? '● Listening…' : connected ? 'Speak or type a command…' : 'Connecting…'}
          disabled={!connected || sending || listening || wakeActive}
          className="flex-1 bg-zinc-800/60 border border-zinc-700 rounded-lg px-3 py-2 text-xs text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-cyan-500/50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        />
        <button
          onClick={() => send(input)}
          disabled={!connected || sending || !input.trim()}
          className="px-3 py-2 rounded-lg bg-cyan-500/20 text-cyan-400 hover:bg-cyan-500/30 border border-cyan-500/30 text-xs transition-all disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Send
        </button>
      </div>

      {(listening || wakeActive) && (
        <div className="mt-1.5 text-center text-[10px] text-cyan-400/60 animate-pulse flex-shrink-0">
          {wakeActive && !listening
            ? 'Wake word detected — speak your command now…'
            : interimText
              ? <span className="text-zinc-200">"{interimText}"</span>
              : 'Listening…'
          }
        </div>
      )}
    </GlassCard>
  )
}

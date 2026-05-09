import { useState, useRef } from 'react'
import { GlassCard } from '../common/GlassCard'

export default function PhantomTerminalPanel() {
  const [mintInput, setMintInput] = useState('')
  const [iframeKey, setIframeKey] = useState(0)
  const [blocked, setBlocked] = useState(false)
  const iframeRef = useRef<HTMLIFrameElement>(null)

  const terminalUrl = mintInput.trim()
    ? `https://trade.phantom.com/?outputMint=${mintInput.trim()}`
    : 'https://trade.phantom.com/'

  function reload() {
    setBlocked(false)
    setIframeKey(k => k + 1)
  }

  // If the iframe loads but has no content height it was blocked — heuristic via onLoad
  function handleLoad() {
    try {
      // Cross-origin: accessing contentDocument throws → that's fine, means it loaded
      iframeRef.current?.contentDocument
    } catch {
      // loaded but cross-origin — good
      return
    }
    // If we can read contentDocument and it's empty → likely blocked
    const doc = iframeRef.current?.contentDocument
    if (doc && (!doc.body || doc.body.innerHTML === '')) {
      setBlocked(true)
    }
  }

  return (
    <GlassCard className="h-full flex flex-col gap-3">
      {/* Header row */}
      <div className="flex items-center justify-between flex-shrink-0">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-400 font-semibold">Phantom Terminal</div>
          <div className="text-zinc-500 text-xs mt-0.5">wallet-connected · top holders · buy position on chart</div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={reload}
            className="text-zinc-600 hover:text-zinc-300 text-xs transition-colors"
            title="Reload"
          >
            ↻
          </button>
          <a
            href={terminalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs px-3 py-1 rounded-md font-semibold transition-all"
            style={{ background: 'rgba(147,51,234,0.15)', border: '1px solid rgba(147,51,234,0.4)', color: '#c084fc' }}
          >
            Open ↗
          </a>
        </div>
      </div>

      {/* Token mint input */}
      <div className="flex gap-2 flex-shrink-0">
        <input
          type="text"
          placeholder="Paste token mint to load specific chart…"
          value={mintInput}
          onChange={e => { setMintInput(e.target.value); setBlocked(false) }}
          className="flex-1 bg-zinc-800/60 border border-zinc-700/50 rounded-md px-3 py-1.5 text-xs font-mono text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-purple-500/50"
        />
        {mintInput && (
          <button
            onClick={() => { setMintInput(''); reload() }}
            className="text-zinc-500 hover:text-zinc-300 text-xs px-2 transition-colors"
          >
            ✕
          </button>
        )}
      </div>

      {/* iframe / blocked fallback */}
      <div className="flex-1 rounded-lg overflow-hidden border border-zinc-700/30 relative min-h-0">
        {blocked ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-zinc-900/95">
            <div className="text-zinc-500 text-sm text-center px-8">
              Phantom Terminal blocked embedding on this page.<br />
              <span className="text-zinc-600 text-xs">Open it directly — your wallet connects automatically.</span>
            </div>
            <a
              href={terminalUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm px-5 py-2.5 rounded-lg font-semibold transition-all"
              style={{ background: 'rgba(147,51,234,0.2)', border: '1px solid rgba(147,51,234,0.5)', color: '#c084fc' }}
            >
              Open Phantom Terminal ↗
            </a>
          </div>
        ) : (
          <iframe
            key={iframeKey}
            ref={iframeRef}
            src={terminalUrl}
            className="w-full h-full"
            title="Phantom Terminal"
            allow="clipboard-read; clipboard-write; web-share"
            referrerPolicy="no-referrer-when-downgrade"
            onLoad={handleLoad}
          />
        )}
      </div>
    </GlassCard>
  )
}

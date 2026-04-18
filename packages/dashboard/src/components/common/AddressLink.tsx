import { useState } from 'react'

interface AddressLinkProps {
  address: string
  truncate?: boolean
  type?: 'account' | 'tx'
}

export function AddressLink({ address, truncate = true, type = 'account' }: AddressLinkProps) {
  const [copied, setCopied] = useState(false)
  const display = truncate ? `${address.slice(0, 4)}...${address.slice(-4)}` : address
  const url = type === 'tx'
    ? `https://solscan.io/tx/${address}`
    : `https://solscan.io/account/${address}`

  const handleCopy = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    navigator.clipboard.writeText(address)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <span className="inline-flex items-center gap-1 font-mono text-xs">
      <a href={url} target="_blank" rel="noopener noreferrer" className="text-slate-300 hover:text-emerald-400 transition-colors">
        {display}
      </a>
      <button onClick={handleCopy} className="text-slate-500 hover:text-slate-300 transition-colors" title="Copy">
        {copied ? (
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
        ) : (
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
        )}
      </button>
    </span>
  )
}

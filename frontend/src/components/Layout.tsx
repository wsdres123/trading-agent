import { useState, useEffect } from 'react'
import { useAuthStore } from '../store/auth'
import { useNavStore, type PageId } from '../store/nav'
import { endpoints } from '../api/endpoints'
import { MarketBar } from './MarketBar'

const PAGES: { id: PageId; label: string; icon: string }[] = [
  { id: 'timing', label: '指数择时', icon: '📈' },
  { id: 'theme', label: '主线模式', icon: '🧭' },
  { id: 'short_term', label: '短线模式', icon: '⚡' },
  { id: 'single_stock', label: '个股模式', icon: '🎯' },
  { id: 'tomorrow', label: '明日推演', icon: '🔮' },
  { id: 'ai_assistant', label: 'AI助手', icon: '🤖' },
  { id: 'eval_report', label: '评测报告', icon: '📊' },
]

export function Layout({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuthStore()
  const { page, setPage } = useNavStore()
  const [health, setHealth] = useState<any>(null)
  const [trading, setTrading] = useState(false)

  useEffect(() => {
    const fetchHealth = async () => {
      try {
        const h = await endpoints.getHealth()
        setHealth(h)
        setTrading(h.trading_hours || false)
      } catch {}
    }
    fetchHealth()
    const timer = setInterval(fetchHealth, 60000)
    return () => clearInterval(timer)
  }, [])

  const sources = health?.source_health || {}

  return (
    <div className="min-h-screen bg-bg flex flex-col">
      {/* Top bar */}
      <div className="flex items-center gap-3 px-4 py-2 bg-panel border-b border-border">
        <h1 className="text-stock font-bold text-lg whitespace-nowrap">
          劫财AI交易
        </h1>
        <MarketBar />
        <div className="flex items-center gap-2 ml-auto text-sm">
          {trading && (
            <span className="chip" style={{ borderColor: '#ff3b3b' }}>
              <span style={{ color: '#ff3b3b' }}>●</span> 交易中
            </span>
          )}
          {Object.entries(sources).map(([name, ok]: [string, any]) => (
            <span key={name} className="text-xs text-muted">
              {name}{ok ? '✓' : '✗'}
            </span>
          ))}
          {user && (
            <span className="chip">
              <span className="k">{user}</span>
            </span>
          )}
          <button
            className="btn text-sm"
            onClick={async () => {
              await endpoints.logout().catch(() => {})
              logout()
              window.location.href = '/login'
            }}
          >
            退出
          </button>
        </div>
      </div>

      {/* Navigation */}
      <div className="flex items-center gap-1 px-4 py-1.5 bg-panel border-b border-border overflow-x-auto flex-nowrap">
        {PAGES.map((p) => (
          <button
            key={p.id}
            onClick={() => setPage(p.id)}
            className={`px-3 py-1.5 rounded-lg text-sm transition-colors whitespace-nowrap ${
              page === p.id
                ? 'bg-bg text-stock border border-stock'
                : 'text-muted hover:text-text border border-transparent'
            }`}
          >
            {p.icon} {p.label}
          </button>
        ))}
      </div>

      {/* Page content */}
      <div className="flex-1 px-4 py-3 overflow-auto">{children}</div>
    </div>
  )
}

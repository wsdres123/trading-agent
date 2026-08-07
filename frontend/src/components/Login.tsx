import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '../store/auth'
import { endpoints } from '../api/endpoints'

export function Login() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()
  const login = useAuthStore((s) => s.login)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await endpoints.login(username, password)
      login(res.token, res.user)
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-bg">
      <div className="w-96 bg-panel border border-border rounded-xl p-8">
        <h1 className="text-2xl font-bold text-stock text-center mb-2">劫财AI交易</h1>
        <p className="text-muted text-sm text-center mb-6">A股智能交易系统</p>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-sm text-muted block mb-1">用户名</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-text focus:border-stock outline-none"
              autoFocus
            />
          </div>
          <div>
            <label className="text-sm text-muted block mb-1">密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-text focus:border-stock outline-none"
            />
          </div>
          {error && <div className="text-down text-sm">{error}</div>}
          <button
            type="submit"
            disabled={loading || !username || !password}
            className="w-full btn-primary py-2.5"
          >
            {loading ? '登录中…' : '登录'}
          </button>
        </form>
      </div>
    </div>
  )
}

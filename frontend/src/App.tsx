import { useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuthStore } from './store/auth'
import { useNavStore } from './store/nav'
import { Layout } from './components/Layout'
import { Login } from './components/Login'
import { Timing } from './pages/Timing'
import { Theme } from './pages/Theme'
import { ShortTerm } from './pages/ShortTerm'
import { SingleStock } from './pages/SingleStock'
import { Tomorrow } from './pages/Tomorrow'
import { AIAssistant } from './pages/AIAssistant'
import { EvalReport } from './pages/EvalReport'
import { endpoints } from './api/endpoints'

function AppContent() {
  const page = useNavStore((s) => s.page)
  switch (page) {
    case 'timing': return <Timing />
    case 'theme': return <Theme />
    case 'short_term': return <ShortTerm />
    case 'single_stock': return <SingleStock />
    case 'tomorrow': return <Tomorrow />
    case 'ai_assistant': return <AIAssistant />
    case 'eval_report': return <EvalReport />
    default: return <Timing />
  }
}

export default function App() {
  const { isAuthed, token, logout } = useAuthStore()

  useEffect(() => {
    if (token) {
      endpoints.me().catch(() => {
        logout()
      })
    }
  }, [token, logout])

  if (!isAuthed()) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  return (
    <Layout>
      <AppContent />
    </Layout>
  )
}

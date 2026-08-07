import { Component, type ReactNode, type ErrorInfo } from 'react'

interface Props { children: ReactNode }
interface State { hasError: boolean; error: Error | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('ErrorBoundary:', error, info)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-bg">
          <div className="w-96 bg-panel border border-border rounded-xl p-8 text-center">
            <h2 className="text-xl font-bold text-down mb-2">页面出错了</h2>
            <p className="text-muted text-sm mb-4">{this.state.error?.message}</p>
            <button className="btn-primary px-4 py-2" onClick={() => window.location.reload()}>
              刷新重试
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

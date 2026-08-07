import { useEffect, useRef, useState, useCallback } from 'react'
import { useMarketStore } from '../store/market'
import { useAuthStore } from '../store/auth'

export interface QuoteData {
  type: 'quote'
  code: string
  data: { 最新价?: number; 涨跌幅?: number; [k: string]: any }
  timestamp: string
}

function wsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = useAuthStore.getState().token || ''
  return `${proto}//${location.host}${path}?token=${encodeURIComponent(token)}`
}

export function useMarketWS() {
  const setData = useMarketStore((s) => s.setData)
  const setStatus = useMarketStore((s) => s.setStatus)
  const wsRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(0)

  const connect = useCallback(() => {
    setStatus('connecting')
    const ws = new WebSocket(wsUrl('/ws/market'))
    wsRef.current = ws

    ws.onopen = () => {
      retryRef.current = 0
      setStatus('connected')
    }
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'market') {
          setData(msg)
        }
      } catch {}
    }
    ws.onclose = () => {
      retryRef.current += 1
      setStatus('reconnecting')
      const delay = Math.min(1000 * 2 ** retryRef.current, 30000)
      setTimeout(connect, delay)
    }
    ws.onerror = () => ws.close()
  }, [setData, setStatus])

  useEffect(() => {
    connect()
    return () => {
      wsRef.current?.close()
      wsRef.current = null
    }
  }, [connect])
}

export function useQuoteWS(code: string | null) {
  const [data, setData] = useState<QuoteData | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (!code) {
      setData(null)
      return
    }
    let retry = 0
    let timer: ReturnType<typeof setTimeout>
    const connect = () => {
      const ws = new WebSocket(wsUrl(`/ws/quotes/${code}`))
      wsRef.current = ws
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.type === 'quote') {
            setData(msg)
            retry = 0
          }
        } catch {}
      }
      ws.onclose = () => {
        retry += 1
        const delay = Math.min(1000 * 2 ** retry, 30000)
        timer = setTimeout(connect, delay)
      }
      ws.onerror = () => ws.close()
    }
    connect()
    return () => {
      clearTimeout(timer)
      wsRef.current?.close()
      wsRef.current = null
    }
  }, [code])

  return data
}

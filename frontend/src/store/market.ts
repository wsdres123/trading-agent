import { create } from 'zustand'

export type WSStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected'

export interface MarketIndex {
  代码: string
  名称: string
  最新价: number
  涨跌幅: number
}

interface MarketState {
  avg_price: number | null
  stock_count: number | null
  indices: MarketIndex[]
  timestamp: string | null
  status: WSStatus
  setData: (d: { avg_price: number | null; stock_count: number | null; indices: MarketIndex[]; timestamp: string }) => void
  setStatus: (s: WSStatus) => void
}

export const useMarketStore = create<MarketState>((set) => ({
  avg_price: null,
  stock_count: null,
  indices: [],
  timestamp: null,
  status: 'disconnected',
  setData: (d) => set(d),
  setStatus: (status) => set({ status }),
}))

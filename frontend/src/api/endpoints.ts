import { apiGet, apiPost, sseStream } from './client'

export const endpoints = {
  // Auth
  login: (username: string, password: string) =>
    apiPost<{ token: string; user: string }>('/login', { username, password }),
  logout: () => apiPost('/logout'),
  me: () => apiGet<{ user: string; role: string }>('/me'),

  // Timing
  getTiming: () => apiGet('/timing'),
  judgeTiming: () => apiPost('/timing/judge'),
  getTimingSignals: () => apiGet('/timing/signals'),
  getAvgPriceKline: (days = 360) => apiGet(`/avg_price_kline?days=${days}`),
  marketTurnover: () => apiGet<{ turnover: number | null }>('/market_turnover'),

  // Emotion
  getEmotion: () => apiGet('/emotion'),
  judgeEmotion: () => apiPost('/emotion/judge'),
  getMarketStats: () => apiGet('/market_stats'),
  getHotStockStats: () => apiGet('/hot_stock_stats'),
  getStatsHistory: (days = 10) => apiGet(`/stats_history?days=${days}`),

  // Index / spot
  getIndexDaily: (symbol: string, days = 1060) =>
    apiGet(`/index_daily/${symbol}?days=${days}`),
  getSpot: () => apiGet('/spot'),
  getIndex: () => apiGet('/index'),
  getHot: () => apiGet('/hot'),
  getQuote: (code: string) => apiGet(`/quote/${code}`),
  getAvgPrice: () => apiGet('/avg_price'),
  getHealth: () => apiGet('/health'),
  getTradeCalendar: () => apiGet('/trade-calendar'),
  getSecurities: (date?: string) =>
    apiGet(`/securities${date ? `?date=${date}` : ''}`),

  // Groups
  getGroups: () => apiGet('/groups'),
  updateGroup: (name: string) => apiPost('/groups', { action: 'update', name }),
  deleteGroup: (name: string) => apiPost('/groups', { action: 'delete', name }),

  // Theme
  getThemeCapacityLimit: () => apiGet('/theme/capacity_limit'),

  // Filter
  parseFilter: (desc: string) => apiPost('/filter', { desc, action: 'parse' }),
  execFilter: (conditions: any[]) => apiPost('/filter', { conditions, action: 'exec' }),
  saveGroup: (name: string, conditions: any[], stocks: any[]) =>
    apiPost('/groups', { action: 'save', name, conditions, stocks }),

  // Chat
  chatStream: (question: string, history: any[], onChunk: (t: string) => void) =>
    sseStream('/chat/stream', { question, history }, onChunk),

  // Eval
  runEval: (mode: 'no_llm' | 'all' | 'filter') =>
    apiPost('/eval/run', { mode }),
}

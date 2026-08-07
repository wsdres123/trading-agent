import { create } from 'zustand'

export type PageId =
  | 'timing'
  | 'theme'
  | 'short_term'
  | 'single_stock'
  | 'tomorrow'
  | 'ai_assistant'
  | 'eval_report'

interface NavState {
  page: PageId
  setPage: (p: PageId) => void
}

export const useNavStore = create<NavState>((set) => ({
  page: 'timing',
  setPage: (page) => set({ page }),
}))

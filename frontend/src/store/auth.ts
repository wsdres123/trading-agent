import { create } from 'zustand'

interface AuthState {
  token: string | null
  user: string | null
  login: (token: string, user: string) => void
  logout: () => void
  isAuthed: () => boolean
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: localStorage.getItem('jc_token'),
  user: localStorage.getItem('jc_user'),
  login: (token, user) => {
    localStorage.setItem('jc_token', token)
    localStorage.setItem('jc_user', user)
    set({ token, user })
  },
  logout: () => {
    localStorage.removeItem('jc_token')
    localStorage.removeItem('jc_user')
    set({ token: null, user: null })
  },
  isAuthed: () => !!get().token,
}))

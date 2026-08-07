import { create } from 'zustand'
import { useEffect } from 'react'

export type ToastType = 'error' | 'success' | 'info'

interface ToastItem {
  id: number
  type: ToastType
  message: string
}

interface ToastState {
  toasts: ToastItem[]
  push: (type: ToastType, message: string) => void
  remove: (id: number) => void
}

let _id = 0

const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  push: (type, message) => {
    const id = ++_id
    set((s) => ({ toasts: [...s.toasts, { id, type, message }] }))
  },
  remove: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

export const toast = {
  error: (msg: string) => useToastStore.getState().push('error', msg),
  success: (msg: string) => useToastStore.getState().push('success', msg),
  info: (msg: string) => useToastStore.getState().push('info', msg),
}

function ToastEntry({ item, onClose }: { item: ToastItem; onClose: () => void }) {
  useEffect(() => {
    const t = setTimeout(onClose, 3000)
    return () => clearTimeout(t)
  }, [onClose])

  const bg = item.type === 'error'
    ? 'bg-red-600 text-white'
    : item.type === 'success'
      ? 'bg-green-600 text-white'
      : 'bg-panel border border-border text-text'

  return (
    <div className={`px-4 py-2 rounded-lg shadow-lg cursor-pointer text-sm ${bg}`} onClick={onClose}>
      {item.message}
    </div>
  )
}

export function ToastContainer() {
  const toasts = useToastStore((s) => s.toasts)
  const remove = useToastStore((s) => s.remove)
  return (
    <div className="fixed top-4 right-4 z-50 space-y-2 max-w-sm">
      {toasts.map((t) => (
        <ToastEntry key={t.id} item={t} onClose={() => remove(t.id)} />
      ))}
    </div>
  )
}

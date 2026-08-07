import { clsx } from 'clsx'

interface ChipProps {
  children: React.ReactNode
  className?: string
}

export function Chip({ children, className }: ChipProps) {
  return <span className={clsx('chip', className)}>{children}</span>
}

export function K({ children }: { children: React.ReactNode }) {
  return <span className="k">{children}</span>
}

interface StatBoxProps {
  label: string
  value: React.ReactNode
  color?: string
  fontSize?: string
  className?: string
  children?: React.ReactNode
}

export function StatBox({ label, value, color, fontSize, className, children }: StatBoxProps) {
  return (
    <div className={`stat-box ${className || ''}`}>
      <div className="label">{label}</div>
      <div className="val" style={{ color, fontSize }}>
        {value}
      </div>
      {children}
    </div>
  )
}

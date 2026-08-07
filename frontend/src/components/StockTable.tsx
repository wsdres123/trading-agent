import { useState, useMemo, useRef, useEffect } from 'react'

export interface Column {
  key: string
  label: string
  pctCol?: boolean
  stockCol?: boolean
  render?: (val: any, row: any) => React.ReactNode
}

interface StockTableProps {
  columns: Column[]
  data: any[]
  height?: number
  rowHeight?: number
}

export function StockTable({ columns, data, height, rowHeight = 38 }: StockTableProps) {
  const [sortCol, setSortCol] = useState<number>(-1)
  const [asc, setAsc] = useState(false)
  const [scrollTop, setScrollTop] = useState(0)
  const containerRef = useRef<HTMLDivElement>(null)
  const [containerH, setContainerH] = useState(height || 400)

  useEffect(() => {
    if (height) {
      setContainerH(height)
    } else if (containerRef.current) {
      setContainerH(containerRef.current.clientHeight || 400)
    }
  }, [height])

  const sorted = useMemo(() => {
    if (sortCol < 0) return data
    const col = columns[sortCol]
    return [...data].sort((a, b) => {
      const av = a[col.key]
      const bv = b[col.key]
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      const an = Number(av)
      const bn = Number(bv)
      let c: number
      if (!isNaN(an) && !isNaN(bn)) {
        c = an - bn
      } else {
        c = String(av).localeCompare(String(bv), 'zh')
      }
      return asc ? c : -c
    })
  }, [data, sortCol, asc, columns])

  const total = sorted.length
  const visibleCount = Math.ceil(containerH / rowHeight) + 5
  const startIdx = Math.floor(scrollTop / rowHeight)
  const endIdx = Math.min(startIdx + visibleCount, total)
  const visible = sorted.slice(startIdx, endIdx)
  const totalH = total * rowHeight
  const offsetY = startIdx * rowHeight

  const handleSort = (i: number) => {
    if (sortCol === i) {
      setAsc(!asc)
    } else {
      setSortCol(i)
      setAsc(false)
    }
  }

  const renderCell = (col: Column, val: any, row: any) => {
    if (col.render) return col.render(val, row)
    if (val == null || (typeof val === 'number' && isNaN(val))) return '-'
    if (typeof val === 'number') {
      if (col.key === '竞价量') return val.toFixed(0)
      return val.toFixed(2)
    }
    return String(val)
  }

  const cellClass = (col: Column, val: any) => {
    if (col.stockCol) return 'stk'
    if (col.pctCol && val != null && !isNaN(Number(val))) {
      const n = Number(val)
      if (n > 0) return 'up'
      if (n < 0) return 'down'
    }
    return ''
  }

  return (
    <div
      ref={containerRef}
      className="overflow-auto border border-border rounded-lg"
      style={{ height: height || `min(${rowHeight * (total + 1) + 4}px, 600px)` }}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
    >
      <table className="w-full border-collapse text-text" style={{ fontSize: '18px' }}>
        <thead className="sticky top-0 z-10">
          <tr>
            {columns.map((col, i) => (
              <th
                key={col.key}
                className="table-th"
                onClick={() => handleSort(i)}
              >
                {col.label}{' '}
                <span className="text-muted text-xs">
                  {sortCol === i ? (asc ? '▲' : '▼') : ''}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {total > 20 ? (
            <>
              <tr style={{ height: offsetY }}><td colSpan={columns.length} /></tr>
              {visible.map((row, vi) => (
                <tr key={vi} className="hover:bg-[#1f1f1f]">
                  {columns.map((col) => {
                    const val = row[col.key]
                    return (
                      <td key={col.key} className={`table-td ${cellClass(col, val)}`}>
                        {renderCell(col, val, row)}
                      </td>
                    )
                  })}
                </tr>
              ))}
              <tr style={{ height: totalH - offsetY - visible.length * rowHeight }}>
                <td colSpan={columns.length} />
              </tr>
            </>
          ) : (
            sorted.map((row, ri) => (
              <tr key={ri} className="hover:bg-[#1f1f1f]">
                {columns.map((col) => {
                  const val = row[col.key]
                  return (
                    <td key={col.key} className={`table-td ${cellClass(col, val)}`}>
                      {renderCell(col, val, row)}
                    </td>
                  )
                })}
              </tr>
            ))
          )}
        </tbody>
      </table>
      {total === 0 && <div className="muted p-4 text-center">（无数据）</div>}
    </div>
  )
}

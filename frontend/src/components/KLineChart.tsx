import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'

export interface KLineMark {
  [date: string]: { signal: string; source?: string }
}

interface KLineChartProps {
  data: {
    日期: string
    开盘: number
    最高: number
    最低: number
    收盘: number
    成交量?: number
  }[]
  title?: string
  height?: number
  marks?: KLineMark
  maLines?: { period: number; label: string; color: string }[]
}

const DEFAULT_MA = [
  { period: 5, label: 'MA5', color: '#f7d774' },
  { period: 10, label: 'MA10', color: '#4dd0e1' },
  { period: 30, label: 'MA30', color: '#ba68c8' },
  { period: 60, label: 'MA60', color: '#90a4ae' },
]

export function KLineChart({
  data,
  title,
  height = 620,
  marks,
  maLines = DEFAULT_MA,
}: KLineChartProps) {
  const hasVol = data.some((d) => d.成交量 != null && !isNaN(d.成交量))

  const dates = data.map((d) => d.日期)
  const ohlc = data.map((d) => [d.开盘, d.收盘, d.最低, d.最高])
  const volumes = data.map((d) => d.成交量 || 0)
  const volColors = data.map((d) =>
    d.收盘 >= d.开盘 ? 'rgba(255,59,59,0.6)' : 'rgba(34,197,94,0.6)',
  )

  const maSeries = maLines.map((ma) => {
    const vals: (number | null)[] = []
    const closes = data.map((d) => d.收盘)
    for (let i = 0; i < closes.length; i++) {
      if (i < ma.period - 1) {
        vals.push(null)
      } else {
        let sum = 0
        for (let j = i - ma.period + 1; j <= i; j++) sum += closes[j]
        vals.push(parseFloat((sum / ma.period).toFixed(2)))
      }
    }
    return {
      name: ma.label,
      type: 'line' as const,
      data: vals,
      smooth: false,
      symbol: 'none',
      lineStyle: { width: 1.5, color: ma.color },
    }
  })

  const markSeries: any[] = []
  if (marks) {
    const d2i: Record<string, number> = {}
    dates.forEach((d, i) => (d2i[d] = i))
    const allHighs = data.map((d) => d.最高)
    const allLows = data.map((d) => d.最低)
    const span = Math.max(...allHighs) - Math.min(...allLows) || 1
    const off = span * 0.012

    for (const [sig, color, pos] of [
      ['多', '#ff3b3b', 'low'],
      ['空', '#22c55e', 'high'],
      ['转', '#ffd500', 'high'],
    ] as [string, string, string][]) {
      const pts: any[] = []
      for (const dstr of Object.keys(marks).sort()) {
        const info = marks[dstr]
        if (info.signal !== sig || !(dstr in d2i)) continue
        const i = d2i[dstr]
        const y = pos === 'low' ? allLows[i] - off : allHighs[i] + off
        pts.push({ value: [i, y], name: sig, itemStyle: { color } })
      }
      if (pts.length) {
        markSeries.push({
          type: 'scatter',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: pts,
          symbol: 'circle',
          symbolSize: 0,
          label: {
            show: true,
            formatter: (p: any) => p.data.name,
            color,
            fontSize: 10,
          },
          tooltip: {
            formatter: (p: any) => {
              const d = dates[p.data.value[0]]
              return `${d} ${sig}（${marks[d]?.source || ''}）`
            },
          },
        })
      }
    }
  }

  const volMa5: (number | null)[] = []
  for (let i = 0; i < volumes.length; i++) {
    if (i < 4) {
      volMa5.push(null)
    } else {
      let sum = 0
      for (let j = i - 4; j <= i; j++) sum += volumes[j]
      volMa5.push(parseFloat((sum / 5).toFixed(2)))
    }
  }

  const option: any = {
    title: title ? { text: title, textStyle: { color: '#fff', fontSize: 14 }, left: 10, top: 4 } : undefined,
    animation: false,
    backgroundColor: '#0e0e0e',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#1a1a1a',
      borderColor: '#333',
      textStyle: { color: '#fff' },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 55, right: 10, top: title ? 36 : 8, bottom: hasVol ? '28%' : '8%' },
      ...(hasVol ? [{ left: 55, right: 10, top: '76%', bottom: 30 }] : []),
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        axisLabel: { color: '#fff', fontSize: 11, rotate: -45, interval: Math.floor(dates.length / 10) },
        axisLine: { lineStyle: { color: '#333' } },
        splitLine: { show: false },
      },
      ...(hasVol
        ? [{
            type: 'category' as const,
            gridIndex: 1,
            data: dates,
            axisLabel: { show: false },
            axisLine: { lineStyle: { color: '#333' } },
          }]
        : []),
    ],
    yAxis: [
      {
        scale: true,
        axisLabel: { color: '#fff', fontSize: 12 },
        axisLine: { lineStyle: { color: '#333' } },
        splitLine: { lineStyle: { color: '#222' } },
      },
      ...(hasVol
        ? [{
            gridIndex: 1,
            axisLabel: { show: false },
            splitLine: { show: false },
          }]
        : []),
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: hasVol ? [0, 1] : 0, start: 70, end: 100 },
      ...(hasVol ? [{ type: 'inside', xAxisIndex: 1, start: 70, end: 100 }] : []),
    ],
    series: [
      {
        type: 'candlestick',
        data: ohlc,
        itemStyle: {
          color: '#ff3b3b',
          color0: '#22c55e',
          borderColor: '#ff3b3b',
          borderColor0: '#22c55e',
        },
      },
      ...maSeries,
      ...(hasVol
        ? [
            {
              type: 'bar',
              xAxisIndex: 1,
              yAxisIndex: 1,
              data: volumes.map((v, i) => ({ value: v, itemStyle: { color: volColors[i] } })),
            },
            {
              type: 'line',
              xAxisIndex: 1,
              yAxisIndex: 1,
              data: volMa5,
              smooth: false,
              symbol: 'none',
              lineStyle: { width: 1.5, color: '#f7d774' },
              name: 'Vol MA5',
            },
          ]
        : []),
      ...markSeries,
    ],
  }

  return (
    <ReactECharts
      option={option as any}
      style={{ height: `${height}px`, width: '100%' }}
      notMerge
      lazyUpdate
    />
  )
}

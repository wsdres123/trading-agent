import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'

interface IntradayData {
  时间: string
  价格: number
  均价?: number
  涨幅?: number
  成交量?: number
  昨收?: number
}

interface IntradayChartProps {
  data: IntradayData[]
  height?: number
}

export function IntradayChart({ data, height = 280 }: IntradayChartProps) {
  const times = data.map((d) => d.时间)
  const prices = data.map((d) => d.价格)
  const avgPrices = data.map((d) => d.均价 || null)
  const volumes = data.map((d) => d.成交量 || 0)
  const prevClose = data[0]?.昨收 || 0

  const volColors = data.map((d, i) => {
    if (i === 0) return 'rgba(34,197,94,0.6)'
    return d.价格 >= data[i - 1].价格 ? 'rgba(255,59,59,0.6)' : 'rgba(34,197,94,0.6)'
  })

  const option: any = {
    animation: false,
    backgroundColor: '#0e0e0e',
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a1a1a',
      borderColor: '#333',
      textStyle: { color: '#fff' },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 42, right: 36, top: 4, bottom: '30%' },
      { left: 42, right: 36, top: '76%', bottom: 4 },
    ],
    xAxis: [
      {
        type: 'category',
        data: times,
        show: false,
        gridIndex: 0,
      },
      {
        type: 'category',
        data: times,
        gridIndex: 1,
        axisLabel: {
          color: '#fff',
          fontSize: 9,
          interval: (i: number) => {
            const t = times[i]
            return ['09:30', '10:30', '11:30', '13:00', '14:00', '15:00'].includes(t)
          },
        },
        axisLine: { lineStyle: { color: '#333' } },
      },
    ],
    yAxis: [
      {
        gridIndex: 0,
        scale: true,
        axisLabel: { color: '#fff', fontSize: 9 },
        splitLine: { lineStyle: { color: '#2a2a2a' } },
      },
      {
        gridIndex: 1,
        axisLabel: { show: false },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        type: 'line',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: prices,
        symbol: 'none',
        lineStyle: { color: '#ffffff', width: 1.2 },
        areaStyle: prevClose
          ? { color: 'rgba(255,59,59,0.10)' }
          : undefined,
      },
      ...(prevClose
        ? [{
            type: 'line' as const,
            xAxisIndex: 0,
            yAxisIndex: 0,
            data: times.map(() => prevClose),
            symbol: 'none',
            lineStyle: { color: '#ffffff', width: 0.8, type: 'dashed' },
          }]
        : []),
      ...(avgPrices.some((v) => v != null)
        ? [{
            type: 'line' as const,
            xAxisIndex: 0,
            yAxisIndex: 0,
            data: avgPrices,
            symbol: 'none',
            lineStyle: { color: '#ffd500', width: 0.8, type: 'dotted' },
          }]
        : []),
      ...(volumes.some((v) => v > 0)
        ? [{
            type: 'bar' as const,
            xAxisIndex: 1,
            yAxisIndex: 1,
            data: volumes.map((v, i) => ({ value: v, itemStyle: { color: volColors[i] } })),
          }]
        : []),
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

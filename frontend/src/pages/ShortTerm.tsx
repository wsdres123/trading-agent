import { useState, useEffect } from 'react'
import { endpoints } from '../api/endpoints'
import { apiPost } from '../api/client'
import { StatBox } from '../components/StatBox'
import { Chip, K as KChip } from '../components/Chip'
import { StockTable, type Column } from '../components/StockTable'
import { KLineChart } from '../components/KLineChart'
import dayjs from 'dayjs'

export function ShortTerm() {
  const [date, setDate] = useState(dayjs().format('YYYY-MM-DD'))
  const [analyzing, setAnalyzing] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [error, setError] = useState('')

  const analyze = async () => {
    setAnalyzing(true)
    setError('')
    try {
      const res = await apiPost('/short_term', { date })
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : '分析失败')
    } finally {
      setAnalyzing(false)
    }
  }

  useEffect(() => {
    if (!result) analyze()
  }, [])

  const sr = result || {}
  const sai = sr.ai_result || {}
  const sev = sr.evidence || {}
  const sld = sr.ladder_df || []

  const ladderCols: Column[] = sld.length
    ? Object.keys(sld[0])
        .map((key) => ({
          key,
          label: key,
          pctCol: key === '涨跌幅',
          stockCol: key === '名称',
        }))
    : []

  const candidateCols: Column[] = [
    { key: '名称', label: '名称', stockCol: true },
    { key: '代码', label: '代码' },
    { key: '连板数', label: '连板数' },
    { key: '所属行业', label: '所属行业' },
    { key: '封单额(亿)', label: '封单额(亿)' },
    { key: '支持数', label: '支持数' },
    { key: '原因', label: '原因' },
  ]

  const isSig = sai.is_signal
  const isCont = sai.is_continuation
  const sigColor = isSig ? '#ff3b3b' : isCont ? '#ffd500' : '#9ca3af'
  const sigIcon = isSig ? '✅' : isCont ? '🔄' : '❌'
  const sigText = isSig ? '起变' : isCont ? '延续' : '无信号'

  return (
    <div className="space-y-3">
      <h3 className="text-lg font-bold text-stock">⚡ 短线模式（连板梯队 · 起变信号 · 情绪博弈）</h3>

      {/* Date + analyze */}
      <div className="flex items-center gap-2">
        <input
          type="date"
          value={date}
          max={dayjs().format('YYYY-MM-DD')}
          onChange={(e) => setDate(e.target.value)}
          className="bg-bg border border-border rounded px-2 py-1 text-sm"
        />
        <button className="btn-primary text-sm" onClick={analyze} disabled={analyzing}>
          {analyzing ? '分析中…' : '🔍 分析起变信号'}
        </button>
      </div>
      {error && <div className="text-down text-sm">{error}</div>}

      {/* 883958 + ladder */}
      <div className="grid grid-cols-3 gap-2">
        <div className="col-span-2">
          <Chip>📊 883958 连板指数 · 起变候选（橙色▲）</Chip>
          {sr.scan_marks && sr.scan_marks.length > 0 && (
            <div className="mt-1">
              <KLineChart883958 marks={sr.scan_marks} date={date} />
            </div>
          )}
          {result && (
            <div className="mt-2">
              <StatBox
                label="883958 信号"
                value={`${sigIcon} ${sigText}`}
                color={sigColor}
              />
              {isSig && sai.signal_reason && (
                <Chip>📝 {sai.signal_reason}</Chip>
              )}
              {isSig && sai.gate_reason && (
                <Chip>🔍 {sai.gate_reason}</Chip>
              )}
              {isCont && sai.continuation_reason && (
                <Chip>🔄 {sai.continuation_reason}</Chip>
              )}
            </div>
          )}
        </div>
        <div className="col-span-1">
          {!result ? (
            <div className="muted">点击「分析起变信号」查看信号判断。</div>
          ) : (
            <>
              <Chip>
                🪜 连板天梯 · {sev.ladder_count || 0}只 · 空间{sev.space || 0}板
              </Chip>
              {sld.length > 0 && ladderCols.length > 0 && (
                <div className="mt-1">
                  <StockTable columns={ladderCols} data={sld} height={220} />
                </div>
              )}
              {!sld.length && (
                <div className="muted text-sm mt-1">
                  ⚠️ 无涨停池数据。东方财富涨停池接口仅保留近20个交易日数据。
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Trigger modes */}
      {result && sai.modes?.filter((m: any) => m.triggered).map((m: any, i: number) => (
        <div key={i}>
          <Chip>
            🔥 <KChip>{m.mode}</KChip> · 买点: {m.buy_point || '-'} · 卖点: {m.sell_point || '-'} · 仓位: {m.position || '-'}
          </Chip>
          {m.candidates?.length > 0 && (
            <div className="mt-1">
              <StockTable
                columns={candidateCols}
                data={m.candidates}
                height={200}
              />
            </div>
          )}
        </div>
      ))}

      {sai.summary && (
        <Chip>📋 总结: <KChip>{sai.summary}</KChip></Chip>
      )}
    </div>
  )
}

function KLineChart883958({ marks, date }: { marks: any[]; date: string }) {
  const [data, setData] = useState<any[]>([])
  useEffect(() => {
    endpoints.getIndexDaily('883958', 120).then((d: any) => {
      if (Array.isArray(d) && d.length) {
        setData(d.sort((a, b) => String(a.日期).localeCompare(String(b.日期))))
      }
    })
  }, [])

  if (!data.length) return null

  // Build marks dict
  const markDict: any = {}
  marks.forEach((m: any) => {
    if (m.date) markDict[m.date] = { signal: '转', source: '起变候选' }
  })

  return (
    <KLineChart
      data={data}
      title="883958 连板指数"
      height={320}
      marks={markDict}
      maLines={[{ period: 5, label: 'MA5', color: '#4dd0e1' }]}
    />
  )
}

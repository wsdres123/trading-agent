import { useState, useEffect, useCallback } from 'react'
import { apiPost } from '../api/client'
import { endpoints } from '../api/endpoints'
import { StatBox } from '../components/StatBox'
import { Chip, K as KChip } from '../components/Chip'
import { StockTable, type Column } from '../components/StockTable'
import { IntradayChart } from '../components/IntradayChart'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'

type TabId = 'mainline' | 'shortterm' | 'zhuanggu' | 'trend_core'

export function SingleStock() {
  const [date, setDate] = useState(dayjs().format('YYYY-MM-DD'))
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [tab, setTab] = useState<TabId>('mainline')
  const [intraCode, setIntraCode] = useState<string | null>(null)
  const [error, setError] = useState('')

  const run = async () => {
    setRunning(true)
    setError('')
    try {
      const res = await apiPost('/single_stock', { date })
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : '筛选失败')
    } finally {
      setRunning(false)
    }
  }

  useEffect(() => {
    if (!result) run()
  }, [])

  const res = result || {}
  const ai = res.ai_result || {}

  const makeCols = (arr: any[]): Column[] =>
    arr?.length
      ? Object.keys(arr[0])
          .map((key) => ({
            key,
            label: key,
            pctCol: ['涨跌幅', '涨速', '区间涨幅', 'ret_5d', 'ret_30d'].includes(key),
            stockCol: key === '名称',
          }))
      : []

  // Stock pairs for intraday
  const stockPairs: [string, string][] = []
  for (const key of ['zhuanggu', 'mainline_display', 'shortterm_display']) {
    const df = res[key]
    if (Array.isArray(df)) {
      df.forEach((r: any) => {
        if (r['代码']) stockPairs.push([String(r['代码']), String(r['名称'] || '')])
      })
    }
  }
  const tc = res.trend_core_data || {}
  if (tc.triggered && Array.isArray(tc.stocks)) {
    tc.stocks.forEach((r: any) => {
      if (r['代码']) stockPairs.push([String(r['代码']), String(r['名称'] || '')])
    })
  }
  const uniquePairs = [...new Map(stockPairs.map((p) => [p[0], p])).values()] as [string, string][]

  const mlData = res.mainline_data || {}
  const stData = res.shortterm_data || {}
  const tcData = res.trend_core_data || {}
  const aiMl = ai.mainline || []
  const aiSt = ai.shortterm || []
  const aiZg = ai.zhuanggu || []

  return (
    <div className="space-y-3">
      <h3 className="text-lg font-bold text-stock">🎯 个股模式（主线/短线/庄股 · AI判断买卖点）</h3>

      {/* Cycle */}
      <CycleBox cyc={res.cycle} />

      {/* Date + run */}
      <div className="flex items-center gap-2">
        <input
          type="date"
          value={date}
          max={dayjs().format('YYYY-MM-DD')}
          min={dayjs().subtract(29, 'day').format('YYYY-MM-DD')}
          onChange={(e) => setDate(e.target.value)}
          className="bg-bg border border-border rounded px-2 py-1 text-sm"
        />
        <button className="btn-primary text-sm" onClick={run} disabled={running}>
          {running ? '筛选中…' : '🔍 筛选+AI判断'}
        </button>
        <span className="muted text-sm">📅 {date} · 连板天梯仅支持30天内数据</span>
      </div>
      {error && <div className="text-down text-sm">{error}</div>}

      {/* AI summary */}
      {ai.summary && (
        <StatBox label="🤖 AI 个股判断总结" value={ai.summary} fontSize="15px" />
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border">
        {([
          ['mainline', '📈 主线模式个股'],
          ['shortterm', '⚡ 短线模式个股'],
          ['zhuanggu', '💰 庄股'],
          ['trend_core', '📊 趋势核心'],
        ] as [TabId, string][]).map(([id, label]) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`px-3 py-1.5 text-sm border-b-2 ${
              tab === id ? 'border-stock text-stock' : 'border-transparent text-muted'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Main line */}
      {tab === 'mainline' && (
        <div>
          {mlData.error === 'need_cache' ? (
            <div className="stat-box">
              <div className="text-down">主线个股需先构建全市场数据缓存（约1-2分钟）。</div>
              <button className="btn mt-2" onClick={() => apiPost('/rebuild-cache')}>🔄 构建缓存</button>
            </div>
          ) : mlData.has_mainline ? (
            <>
              <Chip>主线: <KChip>{mlData.board}</KChip> · {mlData.start}~{mlData.end}</Chip>
              {res.mainline_display?.length > 0 && (
                <>
                  <div className="font-bold mt-1">核心容量个股：</div>
                  <StockTable
                    columns={makeCols(res.mainline_display.filter((r: any) => mlData.core_codes?.includes(r['代码'])))}
                    data={res.mainline_display.filter((r: any) => mlData.core_codes?.includes(r['代码']))}
                    height={200}
                  />
                  <div className="font-bold mt-1">补涨前5：</div>
                  <StockTable
                    columns={makeCols(res.mainline_display.filter((r: any) => mlData.follow_codes?.includes(r['代码'])))}
                    data={res.mainline_display.filter((r: any) => mlData.follow_codes?.includes(r['代码']))}
                    height={178}
                  />
                </>
              )}
              {aiMl.length > 0 && (
                <div className="mt-2">
                  <div className="font-bold">🤖 AI 主线个股判断</div>
                  {aiMl.map((s: any, i: number) => <AIStockCard key={i} s={s} />)}
                </div>
              )}
            </>
          ) : (
            <div className="muted">（当前区间无主线，不弹主线个股。主线一般在A/C周期出现。）</div>
          )}
        </div>
      )}

      {/* Short term */}
      {tab === 'shortterm' && (
        <div>
          {stData.is_signal || stData.is_continuation ? (
            <>
              {stData.is_signal && (
                <>
                  <Chip>起变信号: <KChip>已出现 ✅</KChip></Chip>
                  <Chip>📝 {stData.signal_reason}</Chip>
                </>
              )}
              {stData.is_continuation && (
                <>
                  <Chip>延续信号: <KChip>已触发 🔄</KChip></Chip>
                  <Chip>🔄 {stData.continuation_reason}</Chip>
                </>
              )}
              {stData.modes?.map((m: any, i: number) => (
                <StatBox key={i} label={`🔥 ${m.mode}`} value="" fontSize="14px">
                  <div className="text-sm mt-1">
                    买: <span className="stk">{m.buy_point || '-'}</span> |
                    卖: <span className="stk">{m.sell_point || '-'}</span> |
                    仓位: <span className="stk">{m.position || '-'}</span>
                  </div>
                </StatBox>
              ))}
              {res.shortterm_display?.length > 0 && (
                <StockTable columns={makeCols(res.shortterm_display)} data={res.shortterm_display} height={200} />
              )}
            </>
          ) : (
            <div className="muted">（今日无起变信号 — {stData.signal_reason}）</div>
          )}
          {aiSt.length > 0 && (
            <div className="mt-2">
              <div className="font-bold">🤖 AI 短线个股判断</div>
              {aiSt.map((s: any, i: number) => <AIStockCard key={i} s={s} />)}
            </div>
          )}
        </div>
      )}

      {/* Zhuanggu */}
      {tab === 'zhuanggu' && (
        <div>
          <Chip>筛选: <KChip>连续5日收盘&gt;MA5 · 自由流通&gt;30亿 · 5日涨&gt;20% · 主板 · 30日涨&gt;50%</KChip></Chip>
          {res.zhuanggu?.length > 0 ? (
            <>
              <Chip>命中: <KChip>{res.zhuanggu.length}只</KChip></Chip>
              <StockTable columns={makeCols(res.zhuanggu)} data={res.zhuanggu} height={300} />
            </>
          ) : (
            <div className="muted">（无符合条件的庄股）</div>
          )}
          {aiZg.length > 0 && (
            <div className="mt-2">
              <div className="font-bold">🤖 AI 庄股判断</div>
              {aiZg.map((s: any, i: number) => <AIStockCard key={i} s={s} showRisk />)}
            </div>
          )}
        </div>
      )}

      {/* Trend core */}
      {tab === 'trend_core' && (
        <div>
          {tcData.triggered ? (
            <>
              <div className="flex items-center gap-2">
                <Chip>外部标准: <KChip>{tcData.external?.reason}</KChip></Chip>
                <button className="btn text-xs" onClick={run}>🔄 刷新</button>
              </div>
              {tcData.external?.conditions?.map((c: any, i: number) => (
                <Chip key={i}>
                  {c.ok ? '✅' : '❌'} {c.name}:{' '}
                  <span style={{ color: c.ok ? '#ff3b3b' : '#9ca3af' }}>{c.value}</span>
                </Chip>
              ))}
              {tcData.stocks?.length > 0 ? (
                <>
                  <Chip>命中: <KChip>{tcData.stocks.length}只</KChip> · 自由流通&gt;200亿 · 5日涨&gt;20% · 10日涨&gt;10% · 成交&gt;30亿 · 收盘&gt;3日线</Chip>
                  <StockTable columns={makeCols(tcData.stocks)} data={tcData.stocks} height={300} />
                </>
              ) : (
                <div className="muted">（无符合条件的趋势核心个股）</div>
              )}
            </>
          ) : (
            <div className="muted">（当前有主线或外部条件未满足，需无主线且指数反弹/情绪修复时触发）</div>
          )}
        </div>
      )}

      {/* Intraday */}
      {uniquePairs.length > 0 && (
        <div className="mt-4 border-t border-border pt-3">
          <div className="font-bold text-stock">📈 点击个股查看分时图</div>
          <div className="flex flex-wrap gap-1 mt-1">
            {uniquePairs.map(([code, name]) => (
              <button
                key={code}
                onClick={() => setIntraCode(code)}
                className={`btn text-sm ${intraCode === code ? 'btn-primary' : ''}`}
              >
                {name}
              </button>
            ))}
          </div>
          {intraCode && (
            <div className="mt-2">
              <div className="flex items-center gap-2">
                <button className="btn text-sm" onClick={() => setIntraCode(null)}>✕ 关闭</button>
                <button className="btn text-sm" onClick={run}>🔄 刷新</button>
              </div>
              <IntradayPanel code={intraCode} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function CycleBox({ cyc }: { cyc?: string }) {
  if (cyc === 'D') {
    return (
      <StatBox label="⚠️ 当前中级周期 D" value="D周期不弹个股 — 空仓为主" color="#22c55e" />
    )
  }
  const color = cyc && ['A', 'B', 'C'].includes(cyc) ? '#ff3b3b' : '#9ca3af'
  return (
    <StatBox label={`当前中级周期 ${cyc || '未知'}`} value={cyc && ['A', 'B', 'C'].includes(cyc) ? '✅ 可弹个股' : '待确认'} color={color} />
  )
}

function AIStockCard({ s, showRisk }: { s: any; showRisk?: boolean }) {
  const vc = s.verdict?.includes('可做') ? '#ff3b3b' : s.verdict?.includes('回避') ? '#22c55e' : '#9ca3af'
  return (
    <div className="stat-box mt-1">
      <div>
        <span className="stk">{s.name}</span>({s.code}) —{' '}
        <span style={{ color: vc, fontWeight: 600 }}>{s.verdict}</span>
        {s.type && ` [${s.type}]`}
        {s.mode && ` [${s.mode}]`}
      </div>
      <div className="mt-1">
        <Chip>买: <KChip>{s.buy}</KChip></Chip>
        <Chip>卖: <KChip>{s.sell}</KChip></Chip>
        {showRisk && s.risk && <Chip>⚠️ {s.risk}</Chip>}
      </div>
      {s.reason && <div className="muted mt-1">{s.reason}</div>}
    </div>
  )
}

function IntradayPanel({ code }: { code: string }) {
  const [data, setData] = useState<any[]>([])
  const [loading, setLoading] = useState(true)

  const fetch = useCallback(async () => {
    setLoading(true)
    try {
      const res = await endpoints.getQuote(code)
      const intraday = res.intraday || res.分时 || []
      if (Array.isArray(intraday)) setData(intraday)
    } catch {}
    setLoading(false)
  }, [code])

  useEffect(() => {
    fetch()
  }, [fetch])

  if (loading) return <div className="muted">加载分时数据…</div>
  if (!data.length) return <div className="muted">（无分时数据，可能非交易时段）</div>
  return <IntradayChart data={data} height={280} />
}

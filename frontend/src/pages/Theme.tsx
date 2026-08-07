import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiPost } from '../api/client'
import { endpoints } from '../api/endpoints'
import { StatBox } from '../components/StatBox'
import { Chip, K as KChip } from '../components/Chip'
import { StockTable, type Column } from '../components/StockTable'
import { KLineChart } from '../components/KLineChart'
import dayjs from 'dayjs'

export function Theme() {
  const [start, setStart] = useState(dayjs().subtract(60, 'day').format('YYYY-MM-DD'))
  const [end, setEnd] = useState(dayjs().format('YYYY-MM-DD'))
  const [analyzing, setAnalyzing] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [aiText, setAiText] = useState('')
  const [error, setError] = useState('')

  const detect = async () => {
    setAnalyzing(true)
    setError('')
    setAiText('')
    try {
      const res = await apiPost('/theme', { start, end })
      setResult(res)
      if (!res.error) {
        try {
          const ai = await apiPost('/theme/ai', { data: res })
          setAiText(ai.text || '')
        } catch {}
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '识别失败')
    } finally {
      setAnalyzing(false)
    }
  }

  useEffect(() => {
    if (!result) detect()
  }, [])

  // 同花顺热股指数
  const { data: hot } = useQuery({
    queryKey: ['hot'],
    queryFn: endpoints.getHot,
    staleTime: 30000,
  })

  // 成交前10指数 883902 — 始终展示
  const { data: idxData = [], refetch: refetchIdx } = useQuery({
    queryKey: ['ths-883902'],
    queryFn: () => endpoints.getIndexDaily('883902', 220),
    staleTime: 30000,
  })

  // 昨日容量涨停次日观察
  const { data: capLimit, refetch: refetchCap, isFetching: capFetching, error: capError, isLoading: capLoading } = useQuery({
    queryKey: ['capacity-limit'],
    queryFn: endpoints.getThemeCapacityLimit,
    staleTime: 60000,
  })

  const tr = result || {}
  const hasMainline = tr.has_mainline
  const ml0 = tr.mainlines?.[0]

  const makeStockCols = (arr: any[]): Column[] =>
    arr?.length
      ? Object.keys(arr[0]).map((key) => ({
          key,
          label: key,
          pctCol: ['涨跌幅', '涨速', '区间涨幅', 'ret_5d', 'ret_30d'].includes(key),
          stockCol: key === '名称',
        }))
      : []

  const capCols: Column[] = [
    { key: '名称', label: '名称', stockCol: true },
    { key: '连板数', label: '连板' },
    { key: '昨成交额(亿)', label: '昨成交额(亿)' },
    { key: '流通市值(亿)', label: '流通市值(亿)' },
    { key: '所属行业', label: '行业' },
    { key: '今日涨跌幅', label: '今日涨跌幅', pctCol: true },
    { key: '今日最新价', label: '最新价' },
  ]

  return (
    <div className="space-y-3">
      <h3 className="text-lg font-bold text-stock">🧭 主线模式（趋势A/C周期 · 主线板块与核心/补涨个股）</h3>

      {/* 热股指数参考 */}
      {hot && hot.length > 0 && <HotIndex hot={hot} />}

      {/* 日期筛选 + 识别按钮 */}
      <div className="flex items-center gap-2 flex-wrap">
        <input type="date" value={start} max={dayjs().format('YYYY-MM-DD')}
          onChange={(e) => setStart(e.target.value)}
          className="bg-bg border border-border rounded px-2 py-1 text-sm" />
        <span className="text-muted">~</span>
        <input type="date" value={end} max={dayjs().format('YYYY-MM-DD')}
          onChange={(e) => setEnd(e.target.value)}
          className="bg-bg border border-border rounded px-2 py-1 text-sm" />
        <button className="btn-primary text-sm" onClick={detect} disabled={analyzing}>
          {analyzing ? '识别中…' : '🔍 识别主线'}
        </button>
      </div>
      {error && <div className="text-down text-sm">{error}</div>}

      {tr.error === 'need_cache' && (
        <div className="stat-box">
          <div className="text-down">主线识别需先构建全市场数据缓存（约1-2分钟）。</div>
          <button className="btn mt-2" onClick={() => apiPost('/rebuild-cache')}>🔄 构建全市场数据缓存</button>
        </div>
      )}
      {tr.error === 'no_days' && (
        <div className="text-down text-sm">所选区间内没有可用交易日，请调整区间。</div>
      )}

      {/* 主线识别结果 */}
      {hasMainline && ml0 && (
        <>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <StatBox
                label={`主线判断（${tr.start} ~ ${tr.end} · 主线唯一 · B/D周期无主线）`}
                value={`✅ 唯一主线：${ml0.board}${ml0.ongoing ? '（进行中）' : '（已结束）'}`}
                color="#ff3b3b"
              />
              {ml0.related?.length > 0 && (
                <Chip>🔗 关联板块: <KChip>{ml0.related.slice(0, 8).join('、')}{ml0.related.length > 8 ? '等' : ''}</KChip></Chip>
              )}
              {ml0.secondary?.length > 0 && (
                <Chip>📎 次强板块: <KChip>{ml0.secondary.slice(0, 8).join('、')}</KChip></Chip>
              )}
              {aiText && (
                <Chip>🤖 AI: <KChip>{aiText}</KChip></Chip>
              )}
            </div>
            <div>
              <Chip>
                📊 成交前10指数(883902):{' '}
                <span style={{ color: tr.gate_open_days > 0 ? '#ff3b3b' : '#9ca3af', fontWeight: 700 }}>
                  上升趋势 {tr.gate_open_days}/{tr.gate_total_days} 日
                </span>
              </Chip>
            </div>
          </div>

          {tr.mainlines?.map((ml: any, i: number) => (
            <div key={i}>
              <Chip>
                🔥 主线 <KChip>{ml.board}</KChip> · {ml.start} ~ {ml.end} 连续强 {ml.days} 天
                {ml.ongoing ? '（进行中）' : '（已结束）'} · 单日峰值 {ml.max_count} 只 ·
                门槛{ml.gate_open ? '✅开启' : '⚠未开启'}
              </Chip>
              <div className="grid grid-cols-2 gap-2 mt-1">
                <div>
                  <div className="text-sm font-bold mb-1">⚔️ 趋势核心（阵眼）· 卖点：尾盘破5日线/退潮/跌停/主升2次分歧</div>
                  <StockTable columns={makeStockCols(ml.core)} data={ml.core || []} height={200} />
                </div>
                <div>
                  <div className="text-sm font-bold mb-1">🚀 趋势补涨 · 卖点：尾盘破3日线/退潮</div>
                  <StockTable columns={makeStockCols(ml.follow)} data={ml.follow || []} height={200} />
                </div>
              </div>
            </div>
          ))}

          {tr.daily?.length > 0 && <DailyChart data={tr.daily} />}
        </>
      )}

      {!hasMainline && !tr.error && tr.start && (
        <StatBox
          label={`主线判断（${tr.start} ~ ${tr.end}）`}
          value="❌ 无主线：区间内无板块满足「30日涨幅>50% + 成交额>30亿 的票 ≥3只 且连续强≥5天」"
          color="#9ca3af"
        />
      )}

      {/* 成交前10指数 K线 — 始终展示，至少30天 */}
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-bold">📈 成交前10指数（同花顺883902）· 主线开启参考</span>
          <button className="btn text-xs" onClick={() => refetchIdx()}>🔄</button>
        </div>
        {(idxData as any[]).length > 0 ? (
          <KLineChart
            data={(idxData as any[]).slice(-Math.max(30, (idxData as any[]).length))}
            title={`成交前10指数(883902) · ${(idxData as any[]).length}天 · MA5`}
            height={260}
            maLines={[{ period: 5, label: 'MA5', color: '#ffffff' }]}
          />
        ) : (
          <div className="stat-box text-muted text-sm">加载中…</div>
        )}
      </div>

      {/* 昨日容量涨停次日观察 */}
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-bold">🔭 昨日容量涨停次日观察</span>
          <span className="text-xs text-muted">（昨日涨停 · 成交额&gt;30亿 · 流通市值&gt;200亿）</span>
          <button className="btn text-xs" onClick={() => refetchCap()} disabled={capFetching}>
            {capFetching ? '加载…' : '🔄'}
          </button>
        </div>
        {capError ? (
          <div className="text-down text-sm">加载失败: {capError instanceof Error ? capError.message : String(capError)}</div>
        ) : capLoading ? (
          <div className="text-muted text-sm">加载中…</div>
        ) : capLimit?.error ? (
          <div className="text-muted text-sm">{capLimit.error}</div>
        ) : capLimit?.stocks?.length > 0 ? (
          <>
            <div className="flex gap-2 flex-wrap">
              <Chip>共 <KChip>{capLimit.total}</KChip> 只</Chip>
              <Chip>翻红 <span className="up">{capLimit.up_count}</span> 只
                {capLimit.avg_up != null && <span className="up"> 均+{capLimit.avg_up}%</span>}
              </Chip>
              <Chip>下跌 <span className="down">{capLimit.down_count}</span> 只
                {capLimit.avg_down != null && <span className="down"> 均{capLimit.avg_down}%</span>}
              </Chip>
              <Chip className="text-muted">数据日期: {capLimit.date}</Chip>
            </div>
            <StockTable columns={capCols} data={capLimit.stocks} height={220} />
          </>
        ) : (
          <div className="text-muted text-sm">无数据</div>
        )}
      </div>
    </div>
  )
}

function HotIndex({ hot }: { hot: any[] }) {
  const pcts = hot.map((h) => Number(h.涨跌幅)).filter((n) => !isNaN(n))
  if (!pcts.length) return null
  const idx = pcts.reduce((a, b) => a + b, 0) / pcts.length
  const color = idx > 0 ? '#ff3b3b' : idx < 0 ? '#22c55e' : '#fff'
  return (
    <Chip>
      🔥 同花顺热股指数(当前参考):{' '}
      <span style={{ color, fontWeight: 700 }}>{idx > 0 ? '+' : ''}{idx.toFixed(2)}%</span>
      {' '}· 前10平均涨跌幅
    </Chip>
  )
}

function DailyChart({ data }: { data: any[] }) {
  return (
    <KLineChart
      data={data.map((d: any) => ({
        日期: d.日期,
        开盘: d.候选数,
        最高: d.候选数,
        最低: 0,
        收盘: d.候选数,
      }))}
      title="每日主线候选强度（30日涨幅>50% 且 成交额>30亿 的票数）"
      height={280}
      maLines={[]}
    />
  )
}

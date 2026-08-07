import { useState, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { endpoints } from '../api/endpoints'
import { apiGet } from '../api/client'
import { useMarketStore } from '../store/market'
import { toast } from '../components/Toast'
import { MarketBar } from '../components/MarketBar'
import { StatBox } from '../components/StatBox'
import { Chip, K as KChip } from '../components/Chip'
import { StockTable, type Column } from '../components/StockTable'
import { KLineChart, type KLineMark } from '../components/KLineChart'
import { useAuthStore } from '../store/auth'

const INDEX_OPTIONS: Record<string, string> = {
  '上证指数': 'sh000001',
  '深证成指': 'sz399001',
  '创业板指': 'sz399006',
  '科创50': 'sh000688',
}

const SIGNAL_COLORS: Record<string, string> = {
  '多': '#ff3b3b',
  '空': '#22c55e',
  '转': '#ffd500',
}

export function Timing() {
  const user = useAuthStore((s) => s.user || '')
  const [today] = useState(() => dayjs().format('YYYY-MM-DD'))
  const [indexName, setIndexName] = useState('上证指数')
  const [judging, setJudging] = useState(false)
  const [judgingEmotion, setJudgingEmotion] = useState(false)
  const rtAvgPrice = useMarketStore((s) => s.avg_price)
  const qc = useQueryClient()

  // Timing prediction
  const { data: timing } = useQuery({
    queryKey: ['timing'],
    queryFn: endpoints.getTiming,
    refetchInterval: 60000,
  })

  // Emotion
  const { data: emotion } = useQuery({
    queryKey: ['emotion'],
    queryFn: endpoints.getEmotion,
    refetchInterval: 60000,
  })

  // Timing signals (marks)
  const { data: signalsData } = useQuery({
    queryKey: ['timing-signals'],
    queryFn: endpoints.getTimingSignals,
    staleTime: 300000,
  })
  const marks = (signalsData?.signals || {}) as KLineMark

  // Avg price kline
  const { data: avgKline } = useQuery({
    queryKey: ['avg-price-kline'],
    queryFn: () => endpoints.getAvgPriceKline(360),
    staleTime: 30000,
  })

  // Index daily
  const indexSymbol = INDEX_OPTIONS[indexName]
  const { data: indexData, refetch: refetchIndex } = useQuery({
    queryKey: ['index-daily', indexSymbol],
    queryFn: () => endpoints.getIndexDaily(indexSymbol, 1060),
    staleTime: 30000,
  })

  // Hot stocks
  const { data: hot, refetch: refetchHot } = useQuery({
    queryKey: ['hot-stocks'],
    queryFn: endpoints.getHot,
    staleTime: 30000,
  })

  // Groups
  const { data: groups } = useQuery({
    queryKey: ['groups', user],
    queryFn: endpoints.getGroups,
    staleTime: 60000,
  })

  const handleJudge = async () => {
    setJudging(true)
    try {
      await endpoints.judgeTiming()
      await qc.invalidateQueries({ queryKey: ['timing'] })
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '判断失败')
    } finally {
      setJudging(false)
    }
  }

  const handleJudgeEmotion = async () => {
    setJudgingEmotion(true)
    try {
      await endpoints.judgeEmotion()
      await qc.invalidateQueries({ queryKey: ['emotion'] })
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '判断失败')
    } finally {
      setJudgingEmotion(false)
    }
  }

  const sig = timing?.signal || ''
  const sigColor = SIGNAL_COLORS[sig] || '#9ca3af'
  const est = emotion?.stats || {}
  const epv = emotion?.prev_stats || {}

  // Avg price kline with today's realtime
  const avgData = (() => {
    if (!avgKline?.data) return []
    let d = [...avgKline.data]
    if (rtAvgPrice && d.length > 0 && d[d.length - 1].日期 !== today) {
      const prev = d[d.length - 1]
      d.push({
        日期: today,
        开盘: prev.收盘,
        最高: Math.max(prev.收盘, rtAvgPrice),
        最低: Math.min(prev.收盘, rtAvgPrice),
        收盘: rtAvgPrice,
        成交量: null as any,
      })
    }
    return d
  })()

  const prevAvgClose = (() => {
    if (!avgData.length) return null
    const last = avgData[avgData.length - 1]
    if (last.日期 === today && avgData.length > 1) return avgData[avgData.length - 2].收盘
    return last.收盘
  })()
  const avgGain = (rtAvgPrice && prevAvgClose)
    ? (rtAvgPrice - prevAvgClose) / prevAvgClose * 100
    : null

  const hotColumns: Column[] = hot?.length
    ? Object.keys(hot[0])
        .filter((c) => ['排名', '代码', '名称', '最新价', '涨跌幅', '成交额(亿)', '板块'].includes(c))
        .map((key) => ({
          key,
          label: key,
          pctCol: key === '涨跌幅',
          stockCol: key === '名称',
        }))
    : []

  return (
    <div className="space-y-3">
      {/* Prediction panel */}
      <div className="grid grid-cols-5 gap-2">
        <StatBox label={`今日预判（${today}）`} value={sig || '未判断'} color={sigColor} />
        <StatBox label="中级周期" value={timing?.mid_cycle || '-'} />
        <StatBox label="仓位建议" value={timing?.position || '-'} fontSize="17px" />
        <StatBox
          label="全市场成交额"
          value={timing?.turnover != null ? `${timing.turnover} 万亿` : '-'}
          color={timing?.turnover != null && timing.turnover >= 2.5 ? '#ff3b3b' : '#fff'}
        />
        <div className="stat-box flex items-center justify-center">
          <button className="btn-primary w-full" onClick={handleJudge} disabled={judging}>
            {judging ? '判断中…' : '🤖 立即判断'}
          </button>
        </div>
      </div>

      {timing?.reason && (
        <Chip>AI 依据: <KChip>{timing.reason}</KChip>（{timing.time}）</Chip>
      )}
      {timing?.pattern_hint && (
        <Chip>📐 组合规则: <KChip>{timing.pattern_hint}</KChip></Chip>
      )}
      {timing?.latest_review && (
        <Chip>
          📋 复盘表最新（{timing.latest_review.日期}）: 指数择时{' '}
          <KChip>{timing.latest_review.信号}</KChip> · 中级周期{' '}
          <KChip>{timing.latest_review.中级周期}</KChip> · 情绪{timing.latest_review.情绪周期} ·{' '}
          {timing.latest_review.指数}
        </Chip>
      )}

      {/* Emotion node panel — compact inline row */}
      <div className="flex items-stretch gap-2">
        <div className="bg-panel border border-border rounded-lg px-3 py-2 flex flex-col justify-center min-w-[140px]">
          <div className="text-xs text-muted">今日情绪节点</div>
          <div className="font-bold mt-0.5" style={{ color: '#ffd500', fontSize: '20px' }}>
            {emotion?.node || '未判断'}
          </div>
        </div>
        <div className="bg-panel border border-border rounded-lg px-3 py-2 flex flex-col justify-center min-w-[100px]">
          <div className="text-xs text-muted">打板大面</div>
          <div className="font-bold mt-0.5" style={{
            fontSize: '24px',
            color: est.打板大面数 >= 10 ? '#22c55e' : est.打板大面数 > 0 ? '#ff3b3b' : '#9ca3af'
          }}>
            {est.打板大面数 ?? '—'}
          </div>
          <div className="text-xs text-muted">涨停回落&lt;5%</div>
        </div>
        <div className="bg-panel border border-border rounded-lg px-3 py-2 flex flex-wrap items-center gap-1 flex-1">
          {est && Object.keys(est).length > 0 && (
            <>
              <Chip>大面 <KChip>{est.大面数 ?? '-'}</KChip>
                {epv.大面数 != null && est.大面数 != null && (
                  <span className="text-muted text-xs"> 昨{epv.大面数}→{est.大面数}</span>
                )}
              </Chip>
              <Chip>跌停 <span className="down">{est.跌停数 ?? '-'}</span></Chip>
              <Chip>涨停 <span className="up">{est.涨停数 ?? '-'}</span></Chip>
              <Chip>涨超7% <KChip>{est.涨超7数 ?? '-'}</KChip></Chip>
              <Chip>高度股 <KChip>{est.高度个股数 ?? '-'}</KChip></Chip>
            </>
          )}
          {emotion?.reason && (
            <Chip>🎭 <KChip>{emotion.reason}</KChip> ｜ {emotion.advice}（{emotion.time}）</Chip>
          )}
        </div>
        <div className="bg-panel border border-border rounded-lg px-3 py-2 flex items-center">
          <button className="btn-primary whitespace-nowrap" onClick={handleJudgeEmotion} disabled={judgingEmotion}>
            {judgingEmotion ? '判断中…' : '🎭 判断情绪节点'}
          </button>
        </div>
      </div>

      {/* Hot stocks + groups */}
      {hot && hot.length > 0 && (
        <HotStockIndex hot={hot} />
      )}
      <div className="grid grid-cols-5 gap-2">
        <div className="col-span-3 space-y-2">
          <div className="flex items-center gap-2">
            <button className="btn text-sm" onClick={() => { refetchHot() }}>🔄</button>
          </div>
          {hot && hot.length > 0 && (
            <StockTable columns={hotColumns} data={hot} height={426} />
          )}
        </div>
        <div className="col-span-2">
          <GroupsPanel groups={groups || []} user={user} qc={qc} />
        </div>
      </div>

      {/* Index K-line */}
      <div className="grid grid-cols-6 gap-2 items-center">
        <div className="col-span-5 flex gap-1">
          {Object.keys(INDEX_OPTIONS).map((name) => (
            <button
              key={name}
              onClick={() => setIndexName(name)}
              className={`px-2 py-1 rounded text-sm ${
                indexName === name ? 'text-stock border border-stock' : 'text-muted'
              }`}
            >
              {name}
            </button>
          ))}
        </div>
        <div className="col-span-1">
          <button className="btn w-full text-sm" onClick={() => refetchIndex()}>
            🔄 刷新行情
          </button>
        </div>
      </div>
      {indexData && indexData.length > 0 && (
        <KLineChart
          data={indexData.slice(-1000)}
          title={`${indexName} 日K（${Math.min(indexData.length, 1000)}天 · MA5/10/30/60）`}
          height={620}
        />
      )}

      {/* Avg price K-line */}
      {avgData.length > 0 && (
        <div className="grid grid-cols-6 gap-2">
          <div className="col-span-5">
            <KLineChart
              data={avgData}
              title={`全市场平均股价 日K（${avgData.length}天 · 多空线=MA10${Object.keys(marks).length ? ' · 多空转标记' : ''}）`}
              height={520}
              marks={marks}
              maLines={[{ period: 10, label: '多空线', color: '#4dd0e1' }]}
            />
          </div>
          <div className="col-span-1">
            <div className="stat-box mt-2">
              <div className="label">{rtAvgPrice ? '🔴 实时' : '平均'}平均股价</div>
              <div className="val" style={{ color: rtAvgPrice ? '#ffd500' : '#9ca3af', fontSize: '26px' }}>
                {rtAvgPrice ? rtAvgPrice.toFixed(3) : '—'}
              </div>
              {prevAvgClose != null && (
                <div className="text-center text-xs mt-1">
                  <span className="text-muted">昨: {prevAvgClose.toFixed(2)}</span>
                  {avgGain != null && (
                    <span style={{ color: avgGain >= 0 ? '#ff3b3b' : '#22c55e', fontWeight: 700 }}>
                      {' '}{avgGain >= 0 ? '+' : ''}{avgGain.toFixed(2)}%
                    </span>
                  )}
                </div>
              )}
              <div className="text-center text-xs text-muted mt-2">🔴 WS 实时推送</div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function HotStockIndex({ hot }: { hot: any[] }) {
  const pcts = hot.map((h) => Number(h.涨跌幅)).filter((n) => !isNaN(n))
  if (!pcts.length) return null
  const idx = pcts.reduce((a, b) => a + b, 0) / pcts.length
  const color = idx > 0 ? '#ff3b3b' : idx < 0 ? '#22c55e' : '#fff'
  const bigDown = pcts.filter((p) => p <= -7).length
  const up = pcts.filter((p) => p > 0).length
  return (
    <Chip>
      🔥 同花顺热门个股指数（前10平均涨跌幅）:{' '}
      <span style={{ color, fontWeight: 700 }}>{idx > 0 ? '+' : ''}{idx.toFixed(2)}%</span> · 大跌(≤-7%){' '}
      <span className="down">{bigDown}</span> · 翻红 <span className="up">{up}</span>
    </Chip>
  )
}

function GroupsPanel({ groups, user, qc }: { groups: any[]; user: string; qc: any }) {
  const [selected, setSelected] = useState<string[]>([])
  if (!groups.length) {
    return <div className="muted">（暂无分组——到「AI助手」用一句话筛选并保存分组，这里可自选展示 1-2 个分组）</div>
  }
  const names = groups.map((g) => g.name)
  const show = selected.length > 0 ? selected : names.slice(0, 2)

  return (
    <div>
      <select
        className="w-full bg-bg border border-border rounded px-2 py-1 text-sm mb-1"
        multiple
        value={show}
        onChange={(e) => {
          const opts = Array.from(e.target.selectedOptions).map((o) => o.value)
          setSelected(opts.slice(0, 2))
        }}
      >
        {names.map((n) => (
          <option key={n} value={n}>{n}</option>
        ))}
      </select>
      {show.map((gn) => {
        const g = groups.find((x) => x.name === gn)
        if (!g) return null
        const stocks = g.stocks || []
        const cols: Column[] = stocks.length
          ? Object.keys(stocks[0])
              .filter((c) => ['名称', '最新价', '涨跌幅', '涨速', '成交额(亿)', '概念板块', '竞价量', '自由流通市值(亿)', 'ret_5d', 'ret_30d'].includes(c))
              .map((key) => ({
                key,
                label: key,
                pctCol: ['涨跌幅', '涨速', 'ret_5d', 'ret_30d'].includes(key),
                stockCol: key === '名称',
              }))
          : []
        return (
          <div key={gn} className="mb-2">
            <Chip>
              📁 <KChip>{gn}</KChip> · {stocks.length}只 · 更新 {g.updated_at || '-'}
            </Chip>
            <button
              className="btn text-xs ml-1"
              onClick={async () => {
                await endpoints.updateGroup(gn)
                qc.invalidateQueries({ queryKey: ['groups', user] })
              }}
            >
              🔄 更新
            </button>
            {stocks.length > 0 && cols.length > 0 && (
              <div className="mt-1">
                <StockTable columns={cols} data={stocks} height={178} />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

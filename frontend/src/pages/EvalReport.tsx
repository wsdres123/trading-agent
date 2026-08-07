import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiPost, apiGet } from '../api/client'
import { StatBox } from '../components/StatBox'
import { Chip, K as KChip } from '../components/Chip'

const DATA_TARGETS = [
  ['数据准确性', null, 0, ''],
  ['情绪节点', 'emotion_cases.jsonl', 100, '需标注情绪转折节点(高潮/冰点/修复)，来源: 复盘表.csv'],
  ['指数择时', 'timing_cases.jsonl', 100, '需标注每日择时信号(买入/卖出/观望)，来源: 复盘表.csv'],
  ['输出可靠性', null, 0, '依赖 timing_ai.json / emotion_ai.json'],
  ['真实交易质量', null, 0, '依赖 .data/predictions.jsonl 与未来收益回填'],
  ['筛选NLP', 'filter_nlp.jsonl', 50, '需手写自然语言→筛选条件映射用例'],
  ['RAG检索', 'rag_queries.jsonl', 30, '需手写查询语句及期望命中的知识条目'],
  ['性能基准', null, 0, ''],
  ['工具路由', null, 30, '需手写问题→期望工具映射'],
]

export function EvalReport() {
  const [results, setResults] = useState<Record<string, any> | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const { data: dataStatus } = useQuery({
    queryKey: ['data_status'],
    queryFn: () => apiGet('/data_status'),
    staleTime: 60000,
  })

  const run = async (mode: 'no_llm' | 'all' | 'filter') => {
    setRunning(true)
    setError('')
    try {
      const res = await apiPost('/eval/run', { mode })
      setResults(res.results || res)
      if (res.error) setError(res.error)
    } catch (e) {
      setError(e instanceof Error ? e.message : '运行失败')
    } finally {
      setRunning(false)
    }
  }

  const toggle = (name: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const totalP = results ? sum(results, 'pass') : 0
  const totalF = results ? sum(results, 'fail') : 0
  const total = totalP + totalF
  const rate = total ? `${Math.round((totalP / total) * 100)}%` : '-'

  return (
    <div className="space-y-3">
      <h3 className="text-lg font-bold text-stock">📊 评测报告</h3>

      {/* Run buttons */}
      <div className="grid grid-cols-4 gap-2">
        <button className="btn-primary" onClick={() => run('no_llm')} disabled={running}>
          {running ? '运行中…' : '▶ 运行评测（无费用）'}
        </button>
        <button className="btn-primary" onClick={() => run('all')} disabled={running}>
          ▶ 运行全部（含LLM）
        </button>
        <button className="btn-primary" onClick={() => run('filter')} disabled={running}>
          ▶ 仅筛选NLP
        </button>
        <div className="muted text-sm flex items-center">
          无费用 = 数据准确性 + 情绪/择时 + 输出可靠性 + 交易质量 + 筛选NLP + RAG + 性能基准
        </div>
      </div>
      {error && <div className="text-down text-sm">{error}</div>}

      {results && (
        <>
          {/* Overview */}
          <div className="grid grid-cols-4 gap-2">
            <StatBox label="总通过" value={totalP} />
            <StatBox label="总失败" value={totalF} />
            <StatBox label="通过率" value={rate} />
            <StatBox label="评测维度" value={Object.keys(results).length} />
          </div>

          {/* Data completeness */}
          <div className="stat-box">
            <div className="font-bold mb-2">📋 数据完整性检查</div>
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className="table-th">评测维度</th>
                  <th className="table-th">已有</th>
                  <th className="table-th">目标</th>
                  <th className="table-th">补充建议</th>
                </tr>
              </thead>
              <tbody>
                {DATA_TARGETS.map(([name, fname, target, hint]) => (
                  <tr key={name as string} className="hover:bg-[#1f1f1f]">
                    <td className="table-td">{name as string}</td>
                    <td className="table-td">
                      {dataStatus && dataStatus[name as string] != null
                        ? `${dataStatus[name as string]} 条`
                        : '—'}
                    </td>
                    <td className="table-td">{(target as number) > 0 ? `${target} 条` : '—'}</td>
                    <td className="table-td">{hint as string}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Per-dimension */}
          {Object.entries(results).map(([name, r]) => {
            if (typeof r !== 'object') return null
            const p = r.pass || 0
            const f = r.fail || 0
            const icon = f === 0 && p > 0 ? '🟢' : f > 0 ? '🔴' : '⚪'
            const isExp = expanded.has(name)

            return (
              <div key={name} className="stat-box">
                <button
                  onClick={() => toggle(name)}
                  className="w-full text-left font-bold"
                >
                  {icon} {name} — pass={p} fail={f} {isExp ? '▼' : '▶'}
                </button>
                {isExp && (
                  <div className="mt-2 space-y-2">
                    {/* Metrics */}
                    <div className="flex flex-wrap gap-3 text-sm">
                      {r.accuracy != null && <span>准确率: <strong>{(r.accuracy * 100).toFixed(1)}%</strong></span>}
                      {r.recall_accuracy != null && <span>召回准确率: <strong>{(r.recall_accuracy * 100).toFixed(1)}%</strong></span>}
                      {r.overall_avg != null && <span>LLM质量均分: <strong>{r.overall_avg}/5</strong></span>}
                      {r.schema_validity_rate != null && <span>schema合法率: <strong>{r.schema_validity_rate}%</strong></span>}
                      {r.total != null && <span>总用例: {r.total}</span>}
                      {r.correct != null && <span>正确: {r.correct}</span>}
                    </div>

                    {/* Node/Signal distribution */}
                    {r.node_distribution && (
                      <div>
                        <div className="font-bold text-sm">节点分布:</div>
                        <div className="flex flex-wrap gap-1">
                          {Object.entries(r.node_distribution).map(([k, v]) => (
                            <Chip key={k}>{k}: <KChip>{v as any}</KChip></Chip>
                          ))}
                        </div>
                      </div>
                    )}
                    {r.signal_distribution && (
                      <div>
                        <div className="font-bold text-sm">信号分布:</div>
                        <div className="flex flex-wrap gap-1">
                          {Object.entries(r.signal_distribution).map(([k, v]) => (
                            <Chip key={k}>{k}: <KChip>{v as any}</KChip></Chip>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Trading quality */}
                    {['timing', 'emotion', 'shortterm'].map((tq) => {
                      if (!r[tq] || typeof r[tq] !== 'object') return null
                      const tqData = r[tq]
                      return (
                        <div key={tq}>
                          <div className="font-bold text-sm">{tq} 未来收益评估:</div>
                          <table className="w-full text-sm mt-1">
                            <thead>
                              <tr>
                                <th className="table-th">周期</th>
                                <th className="table-th">样本</th>
                                <th className="table-th">命中率</th>
                                <th className="table-th">平均收益(%)</th>
                                <th className="table-th">失败率</th>
                              </tr>
                            </thead>
                            <tbody>
                              {['d1', 'd3', 'd5'].map((per) => {
                                if (!tqData[per]) return null
                                const pv = tqData[per]
                                return (
                                  <tr key={per}>
                                    <td className="table-td">{per}</td>
                                    <td className="table-td">{pv.total || '-'}</td>
                                    <td className="table-td">{pv.rate ? `${pv.rate}%` : '-'}</td>
                                    <td className="table-td">{pv.avg ?? '-'}</td>
                                    <td className="table-td">{pv.failure_rate ? `${pv.failure_rate}%` : '-'}</td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>
                      )
                    })}

                    {/* Issues sample */}
                    {r.issues_sample?.length > 0 && (
                      <div>
                        <div className="font-bold text-sm">schema 问题样例:</div>
                        {r.issues_sample.slice(0, 5).map((iss: any, i: number) => {
                          const txt = JSON.stringify(iss)
                          return <div key={i} className="text-xs text-muted font-mono">{txt.slice(0, 200)}</div>
                        })}
                      </div>
                    )}

                    {/* Error */}
                    {r.error && <div className="text-down text-sm">错误: {r.error}</div>}
                    {r.note && <div className="text-muted text-sm">{r.note}</div>}
                  </div>
                )}
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}

function sum(obj: Record<string, any>, key: string): number {
  return Object.values(obj).reduce((s: number, v: any) => s + (typeof v === 'object' && v ? v[key] || 0 : 0), 0) as number
}

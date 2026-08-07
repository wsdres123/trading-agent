import { useState, useRef, useEffect } from 'react'
import { apiPost, sseStream } from '../api/client'
import { endpoints } from '../api/endpoints'
import { useAuthStore } from '../store/auth'
import { Chip, K as KChip } from '../components/Chip'
import { StockTable, type Column } from '../components/StockTable'
import { useQuery, useQueryClient } from '@tanstack/react-query'

type Mode = 'chat' | 'filter'

export function AIAssistant() {
  const [mode, setMode] = useState<Mode>('chat')

  return (
    <div className="space-y-3">
      <h3 className="text-lg font-bold text-stock">🤖 AI 助手</h3>
      <div className="flex gap-1">
        {([['chat', '💬 智能问答'], ['filter', '🔬 个股分组筛选']] as [Mode, string][]).map(([id, label]) => (
          <button
            key={id}
            onClick={() => setMode(id)}
            className={`px-3 py-1.5 text-sm rounded-lg border ${
              mode === id ? 'border-stock text-stock' : 'border-border text-muted'
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      {mode === 'chat' ? <ChatPanel /> : <FilterPanel />}
    </div>
  )
}

function ChatPanel() {
  const [messages, setMessages] = useState<{ role: string; content: string }[]>([
    {
      role: 'assistant',
      content: '你好，我是劫财AI交易助手。可向我提问：\n- 交易系统：屠龙表周期、主线筛选、心态管理\n- 实时行情：如「今天上证涨多少」「贵州茅台现价」「半导体板块表现」\n也可在「个股分组筛选」用自然语言描述条件来选股。',
    },
  ])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight)
  }, [messages])

  const send = async () => {
    if (!input.trim() || streaming) return
    const q = input.trim()
    setMessages((m) => [...m, { role: 'user', content: q }])
    setInput('')
    setStreaming(true)
    setMessages((m) => [...m, { role: 'assistant', content: '' }])
    await sseStream(
      '/chat/stream',
      { question: q, history: messages },
      (chunk) => {
        setMessages((prev) => {
          const copy = [...prev]
          copy[copy.length - 1] = { role: 'assistant', content: copy[copy.length - 1].content + chunk }
          return copy
        })
      },
      () => setStreaming(false),
      () => setStreaming(false),
    )
  }

  return (
    <div className="flex flex-col" style={{ height: '70vh' }}>
      <div ref={scrollRef} className="flex-1 overflow-auto space-y-3 pr-2">
        {messages.map((msg, i) => (
          <div
            key={i}
            className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-[80%] rounded-lg px-3 py-2 ${
                msg.role === 'user' ? 'bg-accent text-white' : 'bg-panel border border-border'
              }`}
              style={{ whiteSpace: 'pre-wrap' }}
            >
              {msg.content || (streaming && i === messages.length - 1 ? '…' : '')}
            </div>
          </div>
        ))}
      </div>
      <div className="flex gap-2 mt-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="向AI助手提问…"
          className="flex-1 bg-bg border border-border rounded-lg px-3 py-2 text-text focus:border-stock outline-none"
          disabled={streaming}
        />
        <button className="btn-primary px-6" onClick={send} disabled={streaming || !input.trim()}>
          {streaming ? '…' : '发送'}
        </button>
      </div>
    </div>
  )
}

function FilterPanel() {
  const user = useAuthStore((s) => s.user || '')
  const qc = useQueryClient()
  const [desc, setDesc] = useState('')
  const [parsing, setParsing] = useState(false)
  const [conds, setConds] = useState<any[] | null>(null)
  const [filtering, setFiltering] = useState(false)
  const [results, setResults] = useState<any[] | null>(null)
  const [groupName, setGroupName] = useState('')
  const [msg, setMsg] = useState('')

  const { data: cacheStatus } = useQuery({
    queryKey: ['metrics-cache-status'],
    queryFn: () => apiPost('/filter', { action: 'cache_status' }),
    staleTime: 30000,
  })

  const { data: groups, refetch: refetchGroups } = useQuery({
    queryKey: ['groups', user],
    queryFn: endpoints.getGroups,
    staleTime: 60000,
  })

  const parse = async () => {
    if (!desc.trim()) return
    setParsing(true)
    setMsg('')
    try {
      const res = await apiPost('/filter', { desc, action: 'parse' })
      setConds(res.conditions || [])
      if (!res.conditions?.length) setMsg('未能解析出任何条件，请换一种描述。')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '解析失败')
    } finally {
      setParsing(false)
    }
  }

  const execFilter = async () => {
    if (!conds) return
    setFiltering(true)
    setMsg('')
    try {
      const res = await apiPost('/filter', { conditions: conds, action: 'exec' })
      setResults(res.stocks || [])
      setMsg(`筛选完成，命中 ${res.stocks?.length || 0} 只。`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '筛选失败')
    } finally {
      setFiltering(false)
    }
  }

  const save = async () => {
    if (!groupName.trim() || !results) return
    try {
      await endpoints.saveGroup(groupName.trim(), conds || [], results)
      setMsg(`已保存分组「${groupName.trim()}」。`)
      setGroupName('')
      refetchGroups()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '保存失败')
    }
  }

  const resultCols: Column[] = results?.length
    ? Object.keys(results[0])
        .map((key) => ({
          key,
          label: key,
          pctCol: ['涨跌幅', '涨速', 'ret_5d', 'ret_30d'].includes(key),
          stockCol: key === '名称',
        }))
    : []

  return (
    <div className="space-y-3">
      <div className="muted text-sm">
        用自然语言描述筛选条件，AI 解析后实时筛股并保存为可更新的分组。
      </div>

      {/* Cache status */}
      {cacheStatus && (
        <Chip>
          数据缓存: <KChip>{cacheStatus.exists ? (cacheStatus.fresh ? '已就绪' : '已过期') : '未构建'}</KChip>
          {' '}· {cacheStatus.rows}只 · 截至 {cacheStatus.mtime}
        </Chip>
      )}

      {/* Input + parse */}
      <div className="grid grid-cols-5 gap-2">
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          placeholder="在此输入筛选条件…"
          className="col-span-4 bg-bg border border-border rounded-lg px-3 py-2 text-text focus:border-stock outline-none"
          rows={3}
        />
        <div className="col-span-1 flex items-center justify-center">
          <button className="btn-primary w-full" onClick={parse} disabled={parsing || !desc.trim()}>
            {parsing ? '解析中…' : '🔍 解析并筛选'}
          </button>
        </div>
      </div>

      {msg && <div className="text-sm text-stock">{msg}</div>}

      {/* Conditions */}
      {conds && conds.length > 0 && (
        <>
          <div className="font-bold">解析出的条件：</div>
          <div className="flex flex-wrap gap-1">
            {conds.map((c, i) => (
              <Chip key={i}><KChip>●</KChip> {c.label || JSON.stringify(c)}</Chip>
            ))}
          </div>
          <button className="btn-primary" onClick={execFilter} disabled={filtering}>
            {filtering ? '筛选中…' : '▶️ 执行筛选'}
          </button>
        </>
      )}

      {/* Results */}
      {results && results.length > 0 && (
        <>
          <div className="font-bold">筛选结果</div>
          <StockTable columns={resultCols} data={results} height={400} />
          <div className="flex gap-2 items-center">
            <input
              type="text"
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
              placeholder="分组名称"
              className="bg-bg border border-border rounded px-3 py-1.5 text-sm"
            />
            <button className="btn" onClick={save} disabled={!groupName.trim()}>💾 保存为分组</button>
          </div>
        </>
      )}
      {results && results.length === 0 && <div className="muted">（无符合条件的股票）</div>}

      {/* Saved groups */}
      <div className="border-t border-border pt-3">
        <div className="font-bold text-stock">已保存分组</div>
        {!groups?.length ? (
          <div className="muted">暂无分组，筛选后可保存。</div>
        ) : (
          groups.map((g: any) => <GroupCard key={g.name} g={g} user={user} refetch={refetchGroups} />)
        )}
      </div>
    </div>
  )
}

function GroupCard({ g, user, refetch }: { g: any; user: string; refetch: () => void }) {
  const [show, setShow] = useState(false)
  const stocks = g.stocks || []
  const cols: Column[] = stocks.length
    ? Object.keys(stocks[0])
        .filter((c) => ['代码', '名称', '最新价', '涨跌幅', '涨速', '竞价量', '涨停封单额', '自由流通市值(亿)', '成交额(亿)', '概念板块', '成交额', '流通市值_亿'].includes(c))
        .map((key) => ({
          key,
          label: key,
          pctCol: ['涨跌幅', '涨速'].includes(key),
          stockCol: key === '名称',
        }))
    : []

  return (
    <div className="stat-box mt-2">
      <div className="flex items-center gap-2">
        <span className="text-stock font-bold text-lg">{g.name}</span>
        <span className="muted text-sm">更新于 {g.updated_at || ''}</span>
        <span className="muted text-sm">含 {stocks.length} 只个股</span>
      </div>
      <div className="flex flex-wrap gap-1 mt-1">
        {(g.conditions || []).map((c: any, i: number) => (
          <Chip key={i}>{c.label || JSON.stringify(c)}</Chip>
        ))}
      </div>
      <div className="flex gap-2 mt-2">
        <button className="btn text-sm" onClick={() => setShow(!show)}>👁 {show ? '隐藏' : '查看'}</button>
        <button
          className="btn text-sm"
          onClick={async () => {
            await endpoints.updateGroup(g.name)
            refetch()
          }}
        >
          🔄 更新
        </button>
        <button
          className="btn text-sm"
          onClick={async () => {
            await endpoints.deleteGroup(g.name)
            refetch()
          }}
        >
          🗑 删除
        </button>
      </div>
      {show && stocks.length > 0 && cols.length > 0 && (
        <div className="mt-2">
          <StockTable columns={cols} data={stocks} height={300} />
        </div>
      )}
    </div>
  )
}

import { useMarketWS } from '../api/ws'
import { useMarketStore } from '../store/market'

export function MarketBar() {
  useMarketWS()
  const { avg_price: avg, stock_count, indices, timestamp, status } = useMarketStore()

  if (!avg && status !== 'connected' && status !== 'reconnecting') {
    return (
      <div className="flex items-center gap-3 px-3 py-1.5 bg-panel border border-border rounded-lg text-sm">
        <span className="text-muted">连接行情中…</span>
      </div>
    )
  }

  const avgColor = avg && avg > 0 ? 'text-stock' : 'text-muted'

  return (
    <div className="flex items-center gap-4 px-3 py-1.5 bg-panel border border-border rounded-lg text-sm whitespace-nowrap overflow-x-auto">
      <span className="text-muted">
        {avg != null ? (
          <>
            均价 <span className={avgColor}>{avg.toFixed(3)}</span>
          </>
        ) : (
          '均价 —'
        )}
      </span>
      {stock_count != null && (
        <span className="text-muted text-xs">覆盖 {stock_count} 只</span>
      )}
      {indices?.map((idx) => {
        const color = idx.涨跌幅 > 0 ? 'text-up' : idx.涨跌幅 < 0 ? 'text-down' : 'text-text'
        return (
          <span key={idx.代码} className="text-muted">
            {idx.名称} <span className={color}>{idx.最新价?.toFixed(2)}</span>
            <span className={color}> {idx.涨跌幅 > 0 ? '+' : ''}{idx.涨跌幅?.toFixed(2)}%</span>
          </span>
        )
      })}
      {status === 'reconnecting' && (
        <span className="text-xs text-down px-1.5 py-0.5 border border-down rounded">
          重连中
        </span>
      )}
      {status === 'connecting' && (
        <span className="text-xs text-muted px-1.5 py-0.5 border border-border rounded">
          连接中
        </span>
      )}
      <span className="text-muted text-xs ml-auto">{timestamp}</span>
    </div>
  )
}

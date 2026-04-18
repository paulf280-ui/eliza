import { useDashboardStore } from '../../store/dashboard'
import { StatCard } from './StatCard'

export function PortfolioOverview() {
  const { wallet, risk, positions } = useDashboardStore()

  const equity = wallet?.sol_balance ?? 0
  const dailyPnl = risk?.daily_pnl_sol ?? 0
  const posCount = Object.keys(positions).length
  const profitableCount = Object.values(positions).filter((p) => p.pnl_pct > 0).length

  return (
    <>
      <StatCard
        title="Total Equity"
        value={`${equity.toFixed(4)}`}
        valueClassName="gradient-text"
        subtitle="SOL"
      />
      <StatCard
        title="Daily P&L"
        value={`${dailyPnl >= 0 ? '+' : ''}${dailyPnl.toFixed(4)} SOL`}
        valueClassName={dailyPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}
        trend={wallet && wallet.sol_balance > 0 ? (dailyPnl / wallet.sol_balance) * 100 : undefined}
      />
      <StatCard
        title="Open Positions"
        value={`${posCount}`}
        subtitle={posCount > 0 ? `${profitableCount} profitable` : 'None active'}
        valueClassName="text-slate-100"
      />
    </>
  )
}

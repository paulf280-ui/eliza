import { Header } from './Header'

interface DashboardLayoutProps {
  children: React.ReactNode
}

export function DashboardLayout({ children }: DashboardLayoutProps) {
  return (
    <div className="min-h-screen bg-[#06080d] text-slate-200">
      <Header />
      <main className="grid grid-cols-12 gap-4 p-4">
        {children}
      </main>
    </div>
  )
}

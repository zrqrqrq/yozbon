// 布局：左侧导航 + 顶栏（含语言切换/退出）+ 内容区。响应式：窄屏折叠为顶部标签。
import { useI18n } from '../i18n/index.jsx'
import { useAuth } from '../AuthContext.jsx'
import { StatusBadge } from './ui.jsx'

export default function Layout({ page, setPage, children }) {
  const { t, lang, setLang } = useI18n()
  const { host, logout } = useAuth()

  const nav = [
    { key: 'overview', label: t('nav_overview') },
    { key: 'ais', label: t('nav_ais') },
    { key: 'projects', label: t('nav_projects') },
    { key: 'contracts', label: t('nav_contracts') },
    { key: 'notifications', label: t('nav_notifications') },
    { key: 'plaza', label: t('nav_plaza') },
    { key: 'delegations', label: t('nav_delegations') },
    { key: 'templates', label: t('nav_templates') },
    { key: 'stats', label: t('nav_stats') },
    { key: 'webhooks', label: t('nav_webhooks') },
    { key: 'invites', label: t('nav_invites') },
    { key: 'dm', label: t('nav_dm') },
    { key: 'favorites', label: t('nav_favorites') },
    { key: 'growth', label: t('nav_growth') },
    { key: 'payments', label: t('nav_payments') },
    { key: 'files', label: t('nav_files') },
    ...(host.is_admin ? [
      { key: 'observatory', label: t('nav_observatory') },
      { key: 'workers', label: t('nav_workers') },
      { key: 'wedge', label: 'MVT 楔子' },
      { key: 'ops', label: t('nav_ops') },
      { key: 'system', label: t('nav_system') },
    ] : []),
  ]

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      {/* 侧边栏 */}
      <aside className="md:w-56 md:min-h-screen md:border-r md:border-slate-200 md:bg-white">
        <div className="flex items-center justify-between px-4 py-4">
          <span className="text-lg font-bold text-emerald-700">{t('appName')}</span>
        </div>
        <nav className="flex flex-row md:flex-col gap-1 overflow-x-auto px-2">
          {nav.map((n) => (
            <button
              key={n.key}
              onClick={() => setPage(n.key)}
              className={`whitespace-nowrap rounded-md px-3 py-2 text-left text-sm ${page === n.key ? 'bg-emerald-50 font-medium text-emerald-700' : 'text-slate-600 hover:bg-slate-100'}`}
            >
              {n.label}
            </button>
          ))}
        </nav>
      </aside>

      {/* 主区 */}
      <div className="flex-1">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2">
          <div className="flex items-center gap-3 text-sm text-slate-600">
            {host && (
              <>
                <span className="font-medium text-slate-800">{host.nickname || host.email}</span>
                <StatusBadge status={host.status} />
                <span className="hidden sm:inline text-xs text-slate-400">seat={host.seat_tier} slots={host.ai_slots}</span>
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-ghost !px-2 !py-1 text-xs" onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}>
              {lang === 'zh' ? 'EN' : '中文'}
            </button>
            <button className="btn-ghost !px-2 !py-1 text-xs" onClick={logout}>{t('logout')}</button>
          </div>
        </header>
        <main className="p-4 md:p-6">{children}</main>
      </div>
    </div>
  )
}

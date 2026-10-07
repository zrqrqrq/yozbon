/*
 * Copyright (c) 2026 南京楚曼信息科技有限公司 (Nanjing Chuman Information Technology Co., Ltd.)
 * SPDX-License-Identifier: Apache-2.0
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Commercial usage requires a separate commercial agreement (see COMMERCIAL-TERMS.md).
 */
// 公开站点顶栏：品牌 + 广场/画廊/任务大厅入口；右侧 AI 公开账号 / 宿主登录 / 语言切换。
// 风格沿用 emerald/slate 体系（避免靛蓝/紫）。
import { useI18n } from '../i18n/index.jsx'
import { useAiAuth } from '../AiAuthContext.jsx'
import { navigate } from '../router.js'

export default function PublicNav({ route }) {
  const { t, lang, setLang } = useI18n()
  const { ai, logout } = useAiAuth()

  const links = [
    { to: '/plaza', name: 'nav_pub_plaza' },
    { to: '/gallery', name: 'nav_pub_gallery' },
    { to: '/tasks', name: 'nav_pub_tasks' },
    { to: '/feeds', name: 'nav_pub_feeds' },
    { to: '/leaderboard', name: 'nav_pub_board' },
    { to: '/search', name: 'nav_pub_search' },
    { to: '/reports', name: 'nav_pub_reports' },
  ]
  const active = route?.name

  return (
    <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3">
        <button className="flex items-center gap-2" onClick={() => navigate('#/')}>
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-emerald-600 text-sm font-bold text-white">{t('brandMark')}</span>
          <span className="flex flex-col items-start leading-tight">
            <span className="text-base font-bold text-emerald-700">{t('appBrand')}</span>
            <span className="hidden text-[10px] text-slate-400 sm:block">{t('brandTagline')}</span>
          </span>
        </button>

        <nav className="ml-2 flex items-center gap-1">
          {links.map((l) => (
            <button
              key={l.to}
              onClick={() => navigate('#' + l.to)}
              className={`rounded-md px-3 py-1.5 text-sm ${active === l.to.slice(1) ? 'bg-emerald-50 font-medium text-emerald-700' : 'text-slate-600 hover:bg-slate-100'}`}
            >
              {t(l.name)}
            </button>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-2">
          <button className="btn-ghost !px-2 !py-1 text-xs" onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}>
            {lang === 'zh' ? 'EN' : '中文'}
          </button>
          {ai ? (
            <>
              <span className="hidden text-xs text-slate-500 sm:inline">{ai.name || ai.citizen_id}{t('pub_readonly_tag')}</span>
              <button className="btn-ghost !px-2 !py-1 text-xs" onClick={logout}>{t('logout')}</button>
            </>
          ) : (
            <button className="btn-ghost !px-2 !py-1 text-xs" onClick={() => navigate('#/ai/login')}>{t('nav_pub_ai_entry')}</button>
          )}
          <button className="btn-primary !px-3 !py-1 text-xs" onClick={() => navigate('#/host/login')}>{t('nav_pub_host_entry')}</button>
        </div>
      </div>
    </header>
  )
}

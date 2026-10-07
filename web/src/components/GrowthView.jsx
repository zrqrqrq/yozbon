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
// GrowthView — 成长档案展示（AICardPage 公开成长区 + GrowthPage 宿主侧共用）。
// 字段契约：level / title_zh / title_en / badges[] / xp / xp_needed。
// 语言切换时优先取 title_zh/title_en；无数据（level 为空）时返回 null → 调用方整区隐藏。
import { useI18n } from '../i18n/index.jsx'

export default function GrowthView({ growth }) {
  const { t, lang } = useI18n()
  if (!growth || (growth.level == null && !growth.title_zh && !growth.title_en)) return null

  const title = (lang === 'zh' && growth.title_zh) || growth.title_en || growth.title || `Lv.${growth.level ?? '—'}`
  const badges = Array.isArray(growth.badges) ? growth.badges : []
  const xp = Number(growth.xp || 0)
  const xpNeeded = Number(growth.xp_needed || 0)
  const pct = xpNeeded > 0 ? Math.min(100, Math.round((xp / xpNeeded) * 100)) : 0

  return (
    <div className="card">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-700">{t('g_title_public')}</h3>
        <span className="badge bg-emerald-600 text-white">{t('g_level')} {growth.level ?? '—'}</span>
      </div>
      <p className="mt-1 text-xs text-slate-500">{title}</p>

      {/* XP 进度条 */}
      <div className="mt-3">
        <div className="mb-1 flex justify-between text-xs text-slate-400">
          <span>{t('g_xp')}: {xp}</span>
          <span>{t('g_xp_needed')}: {xpNeeded}</span>
        </div>
        <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
          <div className="h-full rounded-full bg-emerald-500" style={{ width: `${pct}%` }} />
        </div>
        <div className="mt-1 text-right text-xs text-slate-400">{t('g_progress')}: {pct}%</div>
      </div>

      {/* 徽章 */}
      {badges.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-xs text-slate-400">{t('g_badges')}</div>
          <div className="flex flex-wrap gap-2">
            {badges.map((b, i) => {
              const label = typeof b === 'object' ? (b.name || b.label || b.title || JSON.stringify(b)) : String(b)
              return <span key={i} className="badge bg-amber-50 text-amber-700">🏅 {label}</span>
            })}
          </div>
        </div>
      )}
    </div>
  )
}

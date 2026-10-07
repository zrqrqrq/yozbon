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
// 公开任务大厅：GET /api/public/tasks（公开任务节点摘要）。
// 宿主登录后显示「发布任务」入口（跳宿主控制台项目页，发布走现有宿主端点）。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useAuth } from '../AuthContext.jsx'
import { getPublicTasks, normalizeList, centToAC } from '../api.js'
import { navigate } from '../router.js'

export default function TaskHallPage() {
  const { t } = useI18n()
  const { host } = useAuth()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setLoading(true); setError('')
    getPublicTasks({ limit: 30 })
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); setError(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">{t('tasks_title')}</h2>
        <div className="flex gap-2">
          <button className="btn-ghost !px-2 !py-1 text-xs" onClick={load}>{t('refresh')}</button>
          {host
            ? <button className="btn-primary !px-3 !py-1 text-xs" onClick={() => navigate('#/host')}>{t('tasks_publish_entry')}</button>
            : <button className="btn-ghost !px-3 !py-1 text-xs" onClick={() => navigate('#/host/login')}>{t('tasks_login_to_publish')}</button>}
        </div>
      </div>
      <p className="mb-5 text-xs text-slate-400">{t('tasks_sub')}</p>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : error ? <div className="card border-dashed text-sm text-slate-400">{t('pub_endpoint_pending')}（{error}）</div>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('tasks_empty')}</div>
        : (
          <div className="space-y-2">
            {items.map((k) => (
              <div key={k.id ?? k.node_id ?? k.title} className="card">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-slate-800">{k.title || k.name || `task#${k.id ?? ''}`}</span>
                  <span className="badge bg-emerald-50 text-emerald-600">{k.skill || k.skill_tag || k.category || 'general'}</span>
                  <span className="ml-auto text-sm font-semibold text-emerald-700">
                    {k.budget_cent != null ? `${centToAC(k.budget_cent)} AC` : (k.budget ? `${k.budget} AC` : '')}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-500">{k.spec || k.description || k.summary || '—'}</p>
                <div className="mt-2 flex items-center gap-3 text-xs text-slate-400">
                  {k.status && <span className="badge bg-slate-100 text-slate-600">{k.status}</span>}
                  {k.deadline && <span>{t('tasks_deadline')}: {String(k.deadline).replace('T', ' ').slice(0, 19)}</span>}
                  <span className="ml-auto">{t('tasks_how')}</span>
                </div>
              </div>
            ))}
          </div>
        )}
    </div>
  )
}

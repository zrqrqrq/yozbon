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
// 公开搜索 SearchPage（#/search）：GET /api/search?q=&type=task|ai|gallery|plaza&page=
// 公开无登录（apiPublic auth:false）；四类 Tab 筛选 + 分页；404/未就绪时宽容空态。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getSearch, normalizeList } from '../api.js'
import { navigate } from '../router.js'

const TYPES = [
  { key: 'task', tabKey: 's_tab_task' },
  { key: 'ai', tabKey: 's_tab_ai' },
  { key: 'gallery', tabKey: 's_tab_gallery' },
  { key: 'plaza', tabKey: 's_tab_plaza' },
]

export default function SearchPage() {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [submitted, setSubmitted] = useState('')   // 已提交的关键词
  const [type, setType] = useState('task')
  const [page, setPage] = useState(1)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback((kw, tp, pg) => {
    if (!kw) return
    setLoading(true); setError('')
    getSearch({ q: kw, type: tp, page: pg })
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); setError(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(submitted, type, page) }, [submitted, type, page, load])

  function submit() {
    setPage(1)
    setSubmitted(q.trim())
  }

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <h2 className="text-base font-semibold">{t('s_title')}</h2>
      <p className="mb-4 text-xs text-slate-400">{t('s_sub')}</p>

      {/* 搜索框 */}
      <div className="flex gap-2">
        <input
          className="input"
          value={q}
          placeholder={t('s_q_ph')}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
        />
        <button className="btn-primary whitespace-nowrap" onClick={submit}>{t('s_search')}</button>
      </div>

      {/* 类型 Tab */}
      <div className="mt-4 flex flex-wrap gap-2">
        {TYPES.map((tp) => (
          <button
            key={tp.key}
            onClick={() => { setType(tp.key); setPage(1) }}
            className={`badge cursor-pointer ${type === tp.key ? 'bg-emerald-600 text-white' : 'bg-slate-100 text-slate-600'}`}
          >
            {t(tp.tabKey)}
          </button>
        ))}
      </div>

      {/* 结果区 */}
      <div className="mt-4">
        {!submitted ? <div className="card border-dashed text-sm text-slate-400">{t('s_try_after')}</div>
          : loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
          : error ? <div className="card border-dashed text-sm text-slate-400">{t('s_error')}（{error}）</div>
          : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('s_empty')}</div>
          : (
            <>
              <p className="mb-2 text-xs text-slate-400">{items.length} {t('s_results')}</p>
              <div className="space-y-2">
                {items.map((it, i) => <ResultCard key={it.id ?? it.target_id ?? i} item={it} type={type} />)}
              </div>
              {/* 分页 */}
              <div className="mt-4 flex justify-between">
                <button className="btn-ghost !px-3 !py-1 text-xs" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>{t('prev')}</button>
                <span className="text-xs text-slate-400">Page {page}</span>
                <button className="btn-ghost !px-3 !py-1 text-xs" disabled={items.length === 0} onClick={() => setPage((p) => p + 1)}>{t('next')}</button>
              </div>
            </>
          )}
      </div>
    </div>
  )
}

function ResultCard({ item, type }) {
  const { t } = useI18n()
  const title = item.title || item.name || item.display_name || `${type}#${item.ref_id ?? item.id ?? ''}`
  const summary = item.snippet || item.summary || item.description || item.text || item.occupation || ''
  const when = item.created_at || item.updated_at
  const id = item.ref_id ?? item.id ?? item.target_id

  function go() {
    if (type === 'ai' && id != null) navigate(`#/ai/${id}`)
    else if (type === 'gallery' && id != null) navigate(`#/work/${id}`)
    else if (type === 'plaza') navigate('#/plaza')
    else if (type === 'task') navigate('#/tasks')
  }

  return (
    <div className="card flex items-start gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="badge bg-emerald-50 text-emerald-700">{t(`s_tab_${item.kind || type}`)}</span>
          <span className="truncate font-medium text-slate-800">{title}</span>
        </div>
        {summary && <p className="mt-1 line-clamp-2 text-xs text-slate-500">{summary}</p>}
        {when && <p className="mt-1 text-xs text-slate-400">{t('s_updated')}: {String(when).replace('T', ' ').slice(0, 16)}</p>}
      </div>
      <button className="btn-ghost !px-2 !py-1 text-xs" onClick={go}>{t('s_view_detail')}</button>
    </div>
  )
}

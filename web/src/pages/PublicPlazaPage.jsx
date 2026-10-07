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
// 公开广场浏览：匿名可读公开流（GET /api/plaza，默认只回 passed）。
// 宿主登录后叠加发布框与举报（复用现有宿主端点）。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useAuth } from '../AuthContext.jsx'
import { getPlaza, publishPlaza, reportPlaza, normalizeList, ApiError } from '../api.js'
import { useToast } from '../components/ui.jsx'

const TABS = [
  { key: 'all', i18n: 'plaza_tab_all' },
  { key: 'chat', i18n: 'plaza_tab_chat' },
  { key: 'dating', i18n: 'plaza_tab_dating' },
  { key: 'promo', i18n: 'plaza_tab_promo' },
  { key: 'notice', i18n: 'plaza_tab_notice' },
  { key: 'teamup', i18n: 'plaza_tab_teamup' },
]

export default function PublicPlazaPage() {
  const { t } = useI18n()
  const { host } = useAuth()
  const toast = useToast()
  const [tab, setTab] = useState('all')
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [content, setContent] = useState('')
  const [ptype, setPtype] = useState('chat')
  const [publishing, setPublishing] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    const params = { limit: 50 }
    if (tab !== 'all') params.type = tab
    getPlaza(params)
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); toast(e.message, 'error') })
      .finally(() => setLoading(false))
  }, [tab, toast])

  useEffect(() => { load() }, [load])

  async function submitPublish(e) {
    e.preventDefault()
    setPublishing(true)
    try {
      const data = await publishPlaza({ type: ptype, content: content.trim(), visibility: 'public' })
      toast(`ok: ${data.audit_status || ''}`, 'success')
      setContent('')
      load()
    } catch (ex) {
      toast(ex instanceof ApiError ? ex.message : String(ex), 'error')
    } finally { setPublishing(false) }
  }

  async function report(msg) {
    try {
      const data = await reportPlaza(msg.id)
      toast(`${t('plaza_reported_toast')} (${t('plaza_reports')}=${data.report_count ?? '?'})`, 'success')
      load()
    } catch (e) { toast(e.message, 'error') }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4 px-4 py-8">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('plaza_title')}</h2>
        {!host && <span className="text-xs text-slate-400">{t('pub_readonly_hint')}</span>}
      </div>

      {host && (
        <div className="card">
          <form onSubmit={submitPublish} className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <label className="label !mb-0">{t('plaza_type')}</label>
              <select className="input !w-40" value={ptype} onChange={(e) => setPtype(e.target.value)}>
                <option value="chat">{t('plaza_tab_chat')}</option>
                <option value="dating">{t('plaza_tab_dating')}</option>
                <option value="promo">{t('plaza_tab_promo')}</option>
                <option value="notice">{t('plaza_tab_notice')}</option>
                <option value="teamup">{t('plaza_tab_teamup')}</option>
              </select>
            </div>
            <textarea className="input" rows={2} placeholder={t('plaza_content_ph')} value={content} onChange={(e) => setContent(e.target.value)} />
            <div className="flex justify-end">
              <button className="btn-primary" type="submit" disabled={publishing || !content.trim()}>
                {publishing ? t('loading') : t('plaza_publish')}
              </button>
            </div>
          </form>
        </div>
      )}

      <div className="flex flex-wrap gap-1">
        {TABS.map((tb) => (
          <button key={tb.key} onClick={() => setTab(tb.key)}
            className={`rounded-md px-3 py-1 text-sm ${tab === tb.key ? 'bg-emerald-600 text-white' : 'bg-white text-slate-600 border border-slate-200 hover:bg-slate-100'}`}>
            {t(tb.i18n)}
          </button>
        ))}
      </div>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('plaza_empty')}</div>
        : (
          <div className="space-y-2">
            {items.map((m) => (
              <div key={m.id} className="card">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                  <span className="badge bg-slate-100 text-slate-600">{m.actor_type} #{m.actor_id}</span>
                  <span className="badge bg-emerald-100 text-emerald-700">{t(`plaza_tab_${m.type}`) || m.type}</span>
                  <span className="ml-auto">{(m.created_at || '').replace('T', ' ').slice(0, 19)}</span>
                </div>
                <p className="whitespace-pre-wrap text-sm">{m.content}</p>
                {host && (
                  <div className="mt-2 flex items-center justify-end gap-2 text-xs text-slate-400">
                    <span>{t('plaza_reports')}: {m.report_count || 0}</span>
                    <button className="btn-ghost !px-2 !py-0.5 text-xs" onClick={() => report(m)}>{t('plaza_report')}</button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
    </div>
  )
}

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
// N9 通知触达 WebhooksPage：订阅列表/新建(url+secret+事件多选)/删除；secret 仅创建时显示一次。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getWebhooks, createWebhook, deleteWebhook, normalizeList } from '../api.js'
import { Modal, useToast } from '../components/ui.jsx'

const EVENT_KEYS = ['wh_ev_signed', 'wh_ev_delivered', 'wh_ev_settled', 'wh_ev_mentioned', 'wh_ev_dispute', 'wh_ev_new_work']
const EVENT_CODES = ['signed', 'delivered', 'settled', 'mentioned', 'dispute', 'new_work']

export default function WebhooksPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [once, setOnce] = useState(null) // 创建后一次性 secret

  const load = useCallback(() => {
    setLoading(true)
    getWebhooks()
      .then((d) => setItems(normalizeList(d)))
      .catch((e => { setItems([]); toast(e.message, 'error') }))
      .finally(() => setLoading(false))
  }, [toast])

  useEffect(() => { load() }, [load])

  async function remove(id) {
    try { await deleteWebhook(id); toast(t('wh_del_ok'), 'success'); load() }
    catch (e) { toast(e.message, 'error') }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('wh_title')}</h2>
        <button className="btn-primary !px-3 !py-1 text-xs" onClick={() => setCreateOpen(true)}>{t('wh_new')}</button>
      </div>
      <p className="text-xs text-slate-400">{t('wh_sub')}</p>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('wh_empty')}</div>
        : (
          <div className="space-y-2">
            {items.map((w) => (
              <div key={w.id} className="card flex flex-wrap items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-xs text-slate-700">{w.url}</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {(w.events || []).map((e) => <span key={e} className="badge bg-emerald-50 text-emerald-700">{e}</span>)}
                  </div>
                </div>
                <span className={`badge ${w.active ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                  {w.active ? t('wh_active') : t('wh_inactive')}
                </span>
                <button className="btn-danger !px-2 !py-1 text-xs" onClick={() => remove(w.id)}>{t('wh_delete')}</button>
              </div>
            ))}
          </div>
        )}

      {createOpen && <CreateModal onClose={() => { setCreateOpen(false); load() }} onCreated={(r) => { setCreateOpen(false); setOnce(r); load() }} />}
      {once && <OnceSecret info={once} onClose={() => setOnce(null)} />}
    </div>
  )
}

function CreateModal({ onClose, onCreated }) {
  const { t } = useI18n()
  const toast = useToast()
  const [url, setUrl] = useState('')
  const [events, setEvents] = useState(['signed', 'settled'])
  const toggle = (e) => setEvents((arr) => arr.includes(e) ? arr.filter((x) => x !== e) : [...arr, e])

  async function submit(ev) {
    ev.preventDefault()
    try {
      const r = await createWebhook({ url, events })
      toast(t('wh_ok'), 'success')
      onCreated(r)
    } catch (e) { toast(e.message, 'error') }
  }

  return (
    <Modal title={t('wh_new')} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div><label className="label">{t('wh_url')}</label><input className="input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={t('wh_url_ph')} required /></div>
        <div>
          <label className="label">{t('wh_events')}</label>
          <div className="flex flex-wrap gap-2">
            {EVENT_CODES.map((c, i) => (
              <label key={c} className="flex items-center gap-1 text-xs text-slate-600">
                <input type="checkbox" checked={events.includes(c)} onChange={() => toggle(c)} /> {t(EVENT_KEYS[i])}
              </label>
            ))}
          </div>
        </div>
        <button className="btn-primary w-full" type="submit">{t('wh_create')}</button>
      </form>
    </Modal>
  )
}

// secret 仅创建时显示一次
function OnceSecret({ info, onClose }) {
  const { t } = useI18n()
  return (
    <Modal title={t('wh_title')} onClose={onClose}>
      <p className="mb-2 text-xs text-amber-600">{t('wh_secret_once')}</p>
      <div className="rounded bg-slate-900 p-3 font-mono text-sm text-emerald-300 break-all">{info.secret || info.signing_secret || '(secret returned by backend)'}</div>
      <button className="btn-primary mt-3 w-full" onClick={() => navigator.clipboard?.writeText(info.secret || '')}>{t('copy')}</button>
      <button className="btn-ghost mt-2 w-full" onClick={onClose}>{t('close')}</button>
    </Modal>
  )
}

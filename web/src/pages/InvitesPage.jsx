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
// 邀请码 InvitesPage：GET /api/host/invites（列表）+ POST /api/host/invites（生成新码）。
// 新码生成后一次性展示 code 与状态（同 WebhooksPage 的 once 模式）。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getHostInvites, createHostInvite, normalizeList } from '../api.js'
import { Modal, useToast } from '../components/ui.jsx'

export default function InvitesPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [once, setOnce] = useState(null)   // 新生成的码（一次性展示）

  const load = useCallback(() => {
    setLoading(true)
    getHostInvites()
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); toast(e.message, 'error') })
      .finally(() => setLoading(false))
  }, [toast])

  useEffect(() => { load() }, [load])

  async function gen() {
    setCreating(true)
    try {
      const r = await createHostInvite()
      toast(t('inv_created'), 'success')
      setOnce(r)
      load()
    } catch (e) { toast(e.message, 'error') }
    finally { setCreating(false) }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('inv_title')}</h2>
        <button className="btn-primary !px-3 !py-1 text-xs" disabled={creating} onClick={gen}>
          {creating ? t('loading') : t('inv_create')}
        </button>
      </div>
      <p className="text-xs text-slate-400">{t('inv_sub')}</p>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('inv_empty')}</div>
        : (
          <div className="space-y-2">
            {items.map((it, i) => (
              <div key={it.id ?? it.code ?? i} className="card flex flex-wrap items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-sm text-slate-800">{it.code || `#${it.id ?? ''}`}</div>
                  <div className="mt-1 flex flex-wrap gap-2 text-xs text-slate-400">
                    {it.created_at && <span>{t('inv_created_at')}: {String(it.created_at).replace('T', ' ').slice(0, 16)}</span>}
                    {it.expires_at && <span>{t('inv_expires')}: {String(it.expires_at).replace('T', ' ').slice(0, 16)}</span>}
                    {it.used_by && <span>{t('inv_used_by')}: {it.used_by}</span>}
                  </div>
                </div>
                <StatusChip status={it.status} />
              </div>
            ))}
          </div>
        )}

      {once && <OnceCode info={once} onClose={() => setOnce(null)} />}
    </div>
  )
}

function StatusChip({ status }) {
  const { t } = useI18n()
  const key = status === 'used' ? 'inv_used' : status === 'expired' ? 'inv_expired' : 'inv_unused'
  const cls = status === 'used' ? 'bg-slate-200 text-slate-600'
    : status === 'expired' ? 'bg-rose-100 text-rose-700'
    : 'bg-emerald-100 text-emerald-700'
  return <span className={`badge ${cls}`}>{status ? t(key) : t('inv_unused')}</span>
}

// 新码一次性展示
function OnceCode({ info, onClose }) {
  const { t } = useI18n()
  const code = info.code || info.invite_code || '(code returned by backend)'
  return (
    <Modal title={t('inv_title')} onClose={onClose}>
      <p className="mb-2 text-xs text-amber-600">{t('inv_note_once')}</p>
      <div className="break-all rounded bg-slate-900 p-3 font-mono text-sm text-emerald-300">{code}</div>
      <div className="mt-2"><StatusChip status={info.status} /></div>
      <button className="btn-primary mt-3 w-full" onClick={() => navigator.clipboard?.writeText(code)}>{t('inv_copy')}</button>
      <button className="btn-ghost mt-2 w-full" onClick={onClose}>{t('close')}</button>
    </Modal>
  )
}

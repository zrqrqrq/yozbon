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
// 通知中心页：GET /api/host/notifications?limit&offset，类型徽标 + title + 时间，手动刷新。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { api } from '../api.js'
import { useToast } from '../components/ui.jsx'

const PAGE_SIZE = 20

// 通知类型徽标配色（后端 type 为英文码）
const TYPE_COLOR = {
  frozen: 'bg-orange-100 text-orange-700',
  lifecycle: 'bg-slate-200 text-slate-600',
  breach: 'bg-rose-100 text-rose-700',
  credit_change: 'bg-sky-100 text-sky-700',
  accepted: 'bg-indigo-100 text-indigo-700',
  settled: 'bg-emerald-100 text-emerald-700',
  contract_update: 'bg-slate-100 text-slate-600',
  invited: 'bg-sky-100 text-sky-700',
  notice: 'bg-slate-200 text-slate-600',
}
// type 码 → i18n 键（中英双语展示）
const TYPE_LABEL = {
  frozen: 'hp_nt_frozen',
  lifecycle: 'hp_nt_lifecycle',
  breach: 'hp_nt_breach',
  credit_change: 'hp_nt_credit_change',
  accepted: 'hp_nt_accepted',
  settled: 'hp_nt_settled',
  contract_update: 'hp_nt_contract_update',
  invited: 'hp_nt_invited',
  notice: 'hp_nt_notice',
}

export default function NotificationsPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    api('/api/host/notifications', { query: { limit: PAGE_SIZE, offset } })
      .then((d) => { setItems(d.items || []); setTotal(d.total || 0) })
      .catch((e) => toast(e.message, 'error'))
      .finally(() => setLoading(false))
  }, [offset, toast])

  useEffect(() => { load() }, [load])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('hp_notifications_title')} ({total})</h2>
        <button className="btn-ghost" onClick={load} disabled={loading}>{loading ? t('loading') : t('refresh')}</button>
      </div>

      <div className="card divide-y divide-slate-100 p-0">
        {items.length === 0 && !loading && <p className="p-4 text-sm text-slate-400">—</p>}
        {items.map((n) => (
          <div key={n.id} className="flex items-center gap-3 px-4 py-2.5">
            <span className={`badge shrink-0 ${TYPE_COLOR[n.type] || 'bg-slate-100 text-slate-600'}`}>{TYPE_LABEL[n.type] ? t(TYPE_LABEL[n.type]) : n.type}</span>
            <span className="flex-1 text-sm text-slate-800">{n.title}</span>
            <span className="font-mono text-xs text-slate-400">{n.ref || ''}</span>
            <span className="shrink-0 text-xs text-slate-400">{(n.at || '').replace('T', ' ').slice(0, 19)}</span>
          </div>
        ))}
      </div>

      <div className="flex justify-between">
        <button className="btn-ghost" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>{t('prev')}</button>
        <button className="btn-ghost" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>{t('next')}</button>
      </div>
    </div>
  )
}

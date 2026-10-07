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
// 算力节点 WorkersPage：GET /api/host/workers
// 字段：name/type/status/heartbeat_at/load/capabilities；宽容空态。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getHostWorkers, normalizeList } from '../api.js'

export default function WorkersPage() {
  const { t } = useI18n()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setLoading(true); setError('')
    getHostWorkers()
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); setError(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('w_title')}</h2>
        <button className="btn-ghost !px-2 !py-1 text-xs" onClick={load}>{t('refresh')}</button>
      </div>
      <p className="text-xs text-slate-400">{t('w_sub')}</p>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : error ? <div className="card border-dashed text-sm text-slate-400">{t('w_error')}（{error}）</div>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('w_empty')}</div>
        : (
          <div className="card overflow-x-auto p-0">
            <table className="w-full min-w-[720px]">
              <thead className="border-b border-slate-200 bg-slate-50">
                <tr>
                  <th className="th">{t('w_name')}</th>
                  <th className="th">{t('w_type')}</th>
                  <th className="th">{t('w_status')}</th>
                  <th className="th">{t('w_heartbeat')}</th>
                  <th className="th">{t('w_load')}</th>
                  <th className="th">{t('w_caps')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((w, i) => (
                  <tr key={w.node_id ?? w.id ?? w.name ?? i} className="border-b border-slate-100">
                    <td className="td font-medium">{w.name || `#${w.node_id ?? w.id ?? i}`}</td>
                    <td className="td">{w.node_type || w.type || '—'}</td>
                    <td className="td"><StatusDot status={w.status} /></td>
                    <td className="td text-xs text-slate-500">{w.heartbeat_at ? String(w.heartbeat_at).replace('T', ' ').slice(0, 16) : t('w_never')}</td>
                    <td className="td">{w.current_load ?? w.load ?? w.load_percent ?? '—'}</td>
                    <td className="td">
                      <div className="flex flex-wrap gap-1">
                        {(Array.isArray(w.capabilities) ? w.capabilities : w.capabilities ? [w.capabilities] : []).map((c, j) => (
                          <span key={j} className="badge bg-slate-100 text-slate-600">{typeof c === 'object' ? (c.name || c.label || JSON.stringify(c)) : String(c)}</span>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </div>
  )
}

function StatusDot({ status }) {
  const cls = status === 'online' || status === 'active' || status === 'idle' ? 'bg-emerald-500'
    : status === 'busy' || status === 'working' ? 'bg-amber-500'
    : status === 'offline' || status === 'dead' ? 'bg-rose-500'
    : 'bg-slate-300'
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-600">
      <span className={`inline-block h-2 w-2 rounded-full ${cls}`} />
      {status || '—'}
    </span>
  )
}

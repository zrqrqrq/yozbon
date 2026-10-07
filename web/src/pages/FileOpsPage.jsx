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
// 文件治理看板：清理订单（status 过滤 + approve/execute/reject 复核）+ 技能库（只读）+ 情报库（只读）。
// 端点：GET /api/sys/cleanup/orders + POST /api/sys/cleanup/orders/{id}/review
//       GET /api/skills（只读）+ GET /api/intel（只读）
import { useEffect, useState, useCallback } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getCleanupOrders, reviewCleanupOrder, getSkills, getIntel, normalizeList } from '../api.js'
import { useToast } from '../components/ui.jsx'

const ORDER_TABS = [
  { key: '', i18n: 'cleanup_all' },
  { key: 'pending', i18n: 'cleanup_pending' },
  { key: 'reviewed', i18n: 'cleanup_reviewed' },
  { key: 'executed', i18n: 'cleanup_executed' },
  { key: 'rejected', i18n: 'cleanup_rejected' },
]
const ORDER_STATUS_COLOR = {
  pending: 'bg-amber-100 text-amber-700',
  reviewed: 'bg-sky-100 text-sky-700',
  executed: 'bg-emerald-100 text-emerald-700',
  rejected: 'bg-rose-100 text-rose-700',
}

function parseItems(raw) {
  if (Array.isArray(raw)) return raw
  if (typeof raw === 'string' && raw) { try { return JSON.parse(raw) } catch { return [] } }
  return []
}

export default function FileOpsPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [tab, setTab] = useState('')
  const [orders, setOrders] = useState([])
  const [ordersLoading, setOrdersLoading] = useState(true)
  const [skills, setSkills] = useState([])
  const [intel, setIntel] = useState([])
  const [busyId, setBusyId] = useState(null)

  const loadOrders = useCallback(() => {
    setOrdersLoading(true)
    const params = { limit: 50 }
    if (tab) params.status = tab
    getCleanupOrders(params)
      .then((d) => setOrders(normalizeList(d)))
      .catch((e) => { setOrders([]); toast(e.message, 'error') })
      .finally(() => setOrdersLoading(false))
  }, [tab, toast])

  useEffect(() => { loadOrders() }, [loadOrders])

  useEffect(() => {
    getSkills({ limit: 50 }).then((d) => setSkills(normalizeList(d))).catch(() => setSkills([]))
    getIntel({ limit: 50 }).then((d) => setIntel(normalizeList(d))).catch(() => setIntel([]))
  }, [])

  // 复核/执行：POST /api/sys/cleanup/orders/{id}/review {action, reviewer:'human', note?}
  async function review(order, action) {
    if (action === 'execute' && !window.confirm(t('cleanup_execute') + '？')) return
    setBusyId(order.id)
    try {
      await reviewCleanupOrder(order.id, { action, reviewer: 'human' })
      toast(`${action} ok`, 'success')
      loadOrders()
    } catch (e) { toast(e.message, 'error') } finally { setBusyId(null) }
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">{t('files_title')}</h2>

      {/* ===== 清理订单 ===== */}
      <div className="card overflow-x-auto p-0">
        <div className="flex flex-wrap items-center gap-1 border-b border-slate-100 px-3 py-2">
          <h3 className="mr-2 text-sm font-semibold">{t('files_cleanup')}</h3>
          {ORDER_TABS.map((tb) => (
            <button
              key={tb.key || 'all'}
              onClick={() => setTab(tb.key)}
              className={`rounded-md px-2 py-0.5 text-xs ${tab === tb.key ? 'bg-emerald-600 text-white' : 'bg-white text-slate-600 border border-slate-200'}`}
            >
              {t(tb.i18n)}
            </button>
          ))}
        </div>
        <table className="w-full min-w-[760px]">
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="th">ID</th>
              <th className="th">{t('cleanup_submitter')}</th>
              <th className="th">{t('cleanup_items')}</th>
              <th className="th">{t('cleanup_status')}</th>
              <th className="th">{t('created_at')}</th>
              <th className="th">{t('actions')}</th>
            </tr>
          </thead>
          <tbody>
            {ordersLoading && <tr><td colSpan={6} className="td">{t('loading')}</td></tr>}
            {!ordersLoading && orders.length === 0 && <tr><td colSpan={6} className="td text-slate-400">{t('cleanup_empty')}</td></tr>}
            {orders.map((o) => {
              const items = parseItems(o.items_json)
              return (
                <tr key={o.id} className="border-b border-slate-100 align-top">
                  <td className="td font-mono text-xs">{o.id}</td>
                  <td className="td">#{o.submitter_ai_id}</td>
                  <td className="td">
                    <details className="text-xs">
                      <summary className="cursor-pointer text-slate-500">{items.length} {t('hp_items')}</summary>
                      <ul className="mt-1 space-y-0.5">
                        {items.map((it, i) => (
                          <li key={i} className="font-mono text-xs text-slate-600">
                            {it.path} <span className="text-slate-400">[{it.category}]</span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  </td>
                  <td className="td"><span className={`badge ${ORDER_STATUS_COLOR[o.status] || 'bg-slate-100 text-slate-600'}`}>{o.status}</span></td>
                  <td className="td text-xs text-slate-400">{(o.created_at || '').replace('T', ' ').slice(0, 19)}</td>
                  <td className="td">
                    <div className="flex flex-wrap gap-1">
                      {o.status === 'pending' && (
                        <>
                          <button className="btn-primary !px-2 !py-0.5 text-xs" disabled={busyId === o.id} onClick={() => review(o, 'approve')}>{t('cleanup_approve')}</button>
                          <button className="btn-danger !px-2 !py-0.5 text-xs" disabled={busyId === o.id} onClick={() => review(o, 'reject')}>{t('cleanup_reject')}</button>
                        </>
                      )}
                      {o.status === 'reviewed' && (
                        <>
                          <button className="btn-primary !px-2 !py-0.5 text-xs" disabled={busyId === o.id} onClick={() => review(o, 'execute')}>{t('cleanup_execute')}</button>
                          <button className="btn-danger !px-2 !py-0.5 text-xs" disabled={busyId === o.id} onClick={() => review(o, 'reject')}>{t('cleanup_reject')}</button>
                        </>
                      )}
                      {(o.status === 'executed' || o.status === 'rejected') && <span className="text-xs text-slate-300">—</span>}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* ===== 技能库（只读） ===== */}
      <div className="card overflow-x-auto p-0">
        <h3 className="border-b border-slate-100 px-3 py-2 text-sm font-semibold">{t('files_skills')}</h3>
        <table className="w-full min-w-[680px]">
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="th">{t('skill_id')}</th>
              <th className="th">{t('skill_owner')}</th>
              <th className="th">{t('skill_royalty')}</th>
              <th className="th">{t('skill_usage')}</th>
              <th className="th">{t('status')}</th>
            </tr>
          </thead>
          <tbody>
            {skills.length === 0 && <tr><td colSpan={5} className="td text-slate-400">{t('skill_empty')}</td></tr>}
            {skills.map((s) => (
              <tr key={s.id} className="border-b border-slate-100">
                <td className="td font-mono text-xs">{s.skill_id}</td>
                <td className="td">#{s.owner_id}</td>
                <td className="td">{Math.round(Number(s.royalty_rate || 0) * 100)}%</td>
                <td className="td">{s.usage_count || 0}</td>
                <td className="td"><span className={`badge ${s.status === 'active' ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>{s.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* ===== 情报库（只读） ===== */}
      <div className="card overflow-x-auto p-0">
        <h3 className="border-b border-slate-100 px-3 py-2 text-sm font-semibold">{t('files_intel')}</h3>
        <table className="w-full min-w-[760px]">
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="th">{t('intel_type')}</th>
              <th className="th">{t('intel_title')}</th>
              <th className="th">{t('intel_summary')}</th>
              <th className="th">{t('intel_ai_status')}</th>
              <th className="th">{t('created_at')}</th>
            </tr>
          </thead>
          <tbody>
            {intel.length === 0 && <tr><td colSpan={5} className="td text-slate-400">{t('intel_empty')}</td></tr>}
            {intel.map((it) => (
              <tr key={it.id} className="border-b border-slate-100">
                <td className="td"><span className="badge bg-slate-100 text-slate-600">{it.type}</span></td>
                <td className="td font-medium">{it.title}</td>
                <td className="td max-w-md">
                  <div className="truncate text-xs text-slate-500" title={it.summary}>{it.summary}</div>
                  {it.source_url && <a className="text-xs text-emerald-600 hover:underline" href={it.source_url} target="_blank" rel="noreferrer">{t('intel_source')}</a>}
                </td>
                <td className="td"><span className="badge bg-amber-100 text-amber-700">{it.ai_status}</span></td>
                <td className="td text-xs text-slate-400">{(it.collected_at || it.created_at || '').replace('T', ' ').slice(0, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

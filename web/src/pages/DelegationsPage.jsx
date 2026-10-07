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
// 委托管理页：列表 + 创建（选名下 AI / scope 多选 / 单笔限额 / 可选有效期）+ 撤销。
// 端点：GET /api/host/delegations / POST /api/host/delegate / DELETE /api/host/delegations/{id}
//       创建时下拉选 AI 复用 GET /api/host/ais（已存在）。
import { useEffect, useState, useCallback } from 'react'
import { useI18n } from '../i18n/index.jsx'
import {
  getDelegations, createDelegation, revokeDelegation, getHostAis,
  normalizeList, centToAC, ApiError,
} from '../api.js'
import { Modal, useToast } from '../components/ui.jsx'

const SCOPES = ['publish_task', 'accept', 'download', 'plaza']
const SCOPE_I18N = {
  publish_task: 'delegation_scope_publish_task',
  accept: 'delegation_scope_accept',
  download: 'delegation_scope_download',
  plaza: 'delegation_scope_plaza',
}
const STATUS_COLOR = {
  active: 'bg-emerald-100 text-emerald-700',
  revoked: 'bg-slate-200 text-slate-600',
  expired: 'bg-amber-100 text-amber-700',
}
const STATUS_I18N = { active: 'delegation_active', revoked: 'delegation_revoked', expired: 'delegation_expired' }

// scope_json 后端可能直接是数组，也可能是 JSON 字符串，归一化为数组
function parseScope(raw) {
  if (Array.isArray(raw)) return raw
  if (typeof raw === 'string' && raw) { try { return JSON.parse(raw) } catch { return [] } }
  return []
}

export default function DelegationsPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    getDelegations({ limit: 50 })
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); toast(e.message, 'error') })
      .finally(() => setLoading(false))
  }, [toast])

  useEffect(() => { load() }, [load])

  async function revoke(d) {
    if (!window.confirm(t('delegation_confirm_revoke'))) return
    try {
      await revokeDelegation(d.id)
      toast('revoked', 'success')
      load()
    } catch (e) { toast(e.message, 'error') }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('delegations_title')} ({items.length})</h2>
        <button className="btn-primary" onClick={() => setCreateOpen(true)}>{t('delegation_create')}</button>
      </div>

      <div className="card overflow-x-auto p-0">
        <table className="w-full min-w-[760px]">
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="th">ID</th>
              <th className="th">{t('delegation_ai')}</th>
              <th className="th">{t('delegation_scope')}</th>
              <th className="th">{t('delegation_max_amount')}</th>
              <th className="th">{t('delegation_expires')}</th>
              <th className="th">{t('delegation_status')}</th>
              <th className="th">{t('created_at')}</th>
              <th className="th">{t('actions')}</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={8} className="td">{t('loading')}</td></tr>}
            {!loading && items.length === 0 && <tr><td colSpan={8} className="td text-slate-400">{t('delegation_empty')}</td></tr>}
            {items.map((d) => {
              const scopes = parseScope(d.scope_json)
              return (
                <tr key={d.id} className="border-b border-slate-100">
                  <td className="td font-mono text-xs">{d.id}</td>
                  <td className="td font-medium">#{d.ai_id}</td>
                  <td className="td">
                    <div className="flex flex-wrap gap-1">
                      {scopes.length === 0 && <span className="text-slate-400">—</span>}
                      {scopes.map((s) => <span key={s} className="badge bg-slate-100 text-slate-600">{t(SCOPE_I18N[s] || s)}</span>)}
                    </div>
                  </td>
                  <td className="td">{d.max_amount_cent ? `${centToAC(d.max_amount_cent)} AC` : '∞'}</td>
                  <td className="td text-xs text-slate-500">{(d.expires_at || '').replace('T', ' ').slice(0, 19) || '—'}</td>
                  <td className="td"><span className={`badge ${STATUS_COLOR[d.status] || 'bg-slate-100 text-slate-600'}`}>{t(STATUS_I18N[d.status] || 'status')}</span></td>
                  <td className="td text-xs text-slate-400">{(d.created_at || '').replace('T', ' ').slice(0, 19)}</td>
                  <td className="td">
                    {d.status === 'active'
                      ? <button className="btn-danger !px-2 !py-0.5 text-xs" onClick={() => revoke(d)}>{t('delegation_revoke')}</button>
                      : <span className="text-xs text-slate-300">—</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {createOpen && <CreateDelegationModal onClose={() => setCreateOpen(false)} onDone={() => { setCreateOpen(false); load() }} />}
    </div>
  )
}

// ---------------- 新建委托 ----------------
function CreateDelegationModal({ onClose, onDone }) {
  const { t } = useI18n()
  const toast = useToast()
  const [ais, setAis] = useState([])
  const [aiId, setAiId] = useState('')
  const [scopes, setScopes] = useState([])
  const [maxAmount, setMaxAmount] = useState('0')
  const [expires, setExpires] = useState('')
  const [err, setErr] = useState('')

  useEffect(() => {
    getHostAis()
      .then((d) => {
        const list = normalizeList(d)
        setAis(list)
        if (list[0]) setAiId(String(list[0].id))
      })
      .catch((e) => toast(e.message, 'error'))
  }, [])

  function toggleScope(s) {
    setScopes((arr) => arr.includes(s) ? arr.filter((x) => x !== s) : [...arr, s])
  }

  async function submit(e) {
    e.preventDefault()
    setErr('')
    if (!aiId) { setErr(t('delegation_no_ai')); return }
    if (scopes.length === 0) { setErr(t('delegation_scope') + ' ' + t('hp_scope_min_one')); return }
    // POST /api/host/delegate: {ai_id, scope:[...], max_amount_cent?, expires_at?(ISO|null)}
    const body = { ai_id: Number(aiId), scope: scopes, max_amount_cent: Math.round(parseFloat(maxAmount || 0) * 100) }
    body.expires_at = expires ? new Date(expires).toISOString() : null
    try {
      await createDelegation(body)
      toast('delegation created', 'success')
      onDone()
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : String(ex))
    }
  }

  return (
    <Modal title={t('delegation_create')} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div>
          <label className="label">{t('delegation_ai')} *</label>
          <select className="input" value={aiId} onChange={(e) => setAiId(e.target.value)}>
            {ais.length === 0 && <option value="">{t('delegation_no_ai')}</option>}
            {ais.map((a) => <option key={a.id} value={a.id}>#{a.id} {a.name}</option>)}
          </select>
        </div>
        <div>
          <label className="label">{t('delegation_scope')} *</label>
          <div className="space-y-1 rounded border border-slate-200 p-2">
            {SCOPES.map((s) => (
              <label key={s} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={scopes.includes(s)} onChange={() => toggleScope(s)} />
                {t(SCOPE_I18N[s])} <span className="text-xs text-slate-400">({s})</span>
              </label>
            ))}
          </div>
        </div>
        <div><label className="label">{t('delegation_max_amount')}</label><input className="input" type="number" step="0.01" min="0" value={maxAmount} onChange={(e) => setMaxAmount(e.target.value)} /></div>
        <div><label className="label">{t('delegation_expires')}</label><input className="input" type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} /></div>
        {err && <p className="text-sm text-rose-600">{err}</p>}
        <button className="btn-primary w-full" type="submit">{t('save')}</button>
      </form>
    </Modal>
  )
}

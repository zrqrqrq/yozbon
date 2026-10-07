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
// 验收/合约页：GET /api/host/contracts（支持 ?status= 过滤 + limit/offset 分页）。
// 对 status∈{delivered, disputed} 的合约显示「验收通过」按钮 →
// POST /api/host/acceptance/{contract_id}，请求体 {result:"accept", reason_json:"[]"}
// （严格对齐 backend/app/routers/host_acceptance.py 的 HostAcceptanceBody）。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { api, centToAC, getContractFlags, setContractFlags } from '../api.js'
import { Modal, ProjectBadge, useToast } from '../components/ui.jsx'

const PAGE_SIZE = 20

export default function ContractsPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')   // 空=全部
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState(null)
  const [flagsTarget, setFlagsTarget] = useState(null)

  const load = useCallback(() => {
    setLoading(true)
    const query = { limit: PAGE_SIZE, offset }
    if (statusFilter) query.status = statusFilter
    api('/api/host/contracts', { query })
      .then((d) => { setItems(d.items || []); setTotal(d.total || 0) })
      .catch((e) => toast(e.message, 'error'))
      .finally(() => setLoading(false))
  }, [offset, statusFilter, toast])

  useEffect(() => { load() }, [load])

  // 验收通过：result=accept，reason_json 默认 "[]"
  async function accept(c) {
    setBusyId(c.contract_id)
    try {
      await api(`/api/host/acceptance/${c.contract_id}`, {
        method: 'POST',
        body: { result: 'accept', reason_json: '[]' },
      })
      toast(`contract ${c.contract_id} accepted`, 'success')
      load()
    } catch (e) { toast(e.message, 'error') }
    finally { setBusyId(null) }
  }

  // 仅 delivered / disputed 可验收（蓝图规则 14：托管锁定待宿主拍板）
  const canAccept = (s) => s === 'delivered' || s === 'disputed'

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">{t('hp_contracts_title')} ({total})</h2>
        <div className="flex gap-2">
          <select className="input !w-auto" value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setOffset(0) }}>
            <option value="">{t('hp_all_status')}</option>
            <option value="escrowed">escrowed</option>
            <option value="executing">executing</option>
            <option value="delivered">delivered</option>
            <option value="accepted">accepted</option>
            <option value="disputed">disputed</option>
            <option value="breached">breached</option>
            <option value="refunded">refunded</option>
          </select>
          <button className="btn-ghost" onClick={load} disabled={loading}>{loading ? t('loading') : t('refresh')}</button>
        </div>
      </div>

      <div className="card overflow-x-auto p-0">
        <table className="w-full min-w-[760px]">
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="th">contract_id</th>
              <th className="th">{t('hp_col_project_title')}</th>
              <th className="th">status</th>
              <th className="th">{t('hp_col_escrow_ac')}</th>
              <th className="th">worker→buyer</th>
              <th className="th">{t('hp_col_created')}</th>
              <th className="th">{t('actions')}</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={7} className="td">{t('loading')}</td></tr>}
            {!loading && items.length === 0 && <tr><td colSpan={7} className="td text-slate-400">—</td></tr>}
            {items.map((c) => (
              <tr key={c.contract_id} className="border-b border-slate-100">
                <td className="td font-mono text-xs">{c.contract_id}</td>
                <td className="td">{c.title || `project#${c.project_id}`}</td>
                <td className="td"><ProjectBadge status={c.status} /></td>
                <td className="td">{centToAC(c.escrow_cent)}</td>
                <td className="td text-xs text-slate-500">{c.worker_id} → {c.buyer_id}</td>
                <td className="td text-xs text-slate-400">{(c.created_at || '').replace('T', ' ').slice(0, 19)}</td>
                <td className="td">
                  <div className="flex gap-1">
                    {canAccept(c.status) && (
                      <button className="btn-primary !px-2 !py-0.5 text-xs" disabled={busyId === c.contract_id} onClick={() => accept(c)}>
                        {busyId === c.contract_id ? t('loading') : t('hp_accept')}
                      </button>
                    )}
                    <button className="btn-ghost !px-2 !py-0.5 text-xs" onClick={() => setFlagsTarget(c)}>{t('flag_showcase')}/{t('flag_ai_broadcast')}</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex justify-between">
        <button className="btn-ghost" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>{t('prev')}</button>
        <button className="btn-ghost" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>{t('next')}</button>
      </div>

      {flagsTarget && <FlagsModal contract={flagsTarget} onClose={() => setFlagsTarget(null)} />}
    </div>
  )
}

function FlagsModal({ contract, onClose }) {
  const { t } = useI18n()
  const toast = useToast()
  const [showcase, setShowcase] = useState(contract.showcase_enabled ?? false)
  const [aiBroadcast, setAiBroadcast] = useState(contract.ai_broadcast_enabled ?? false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  // 读取当前 flags（若合约列表已带则直接使用，否则额外拉取）
  useEffect(() => {
    if (contract.showcase_enabled !== undefined || contract.ai_broadcast_enabled !== undefined) {
      setShowcase(!!contract.showcase_enabled)
      setAiBroadcast(!!contract.ai_broadcast_enabled)
      setLoading(false)
    } else {
      getContractFlags(contract.contract_id)
        .then((d) => { setShowcase(!!d.showcase_enabled); setAiBroadcast(!!d.ai_broadcast_enabled) })
        .catch(() => {})
        .finally(() => setLoading(false))
    }
  }, [contract])

  async function save() {
    setSaving(true)
    try {
      await setContractFlags(contract.contract_id, { showcase_enabled: showcase, ai_broadcast_enabled: aiBroadcast })
      toast(t('flag_updated'), 'success')
      onClose()
    } catch (e) { toast(`${t('flag_update_fail')}: ${e.message}`, 'error') }
    finally { setSaving(false) }
  }

  return (
    <Modal title={`${t('flag_showcase')} / ${t('flag_ai_broadcast')} — #${contract.contract_id}`} onClose={onClose}>
      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p> : (
        <div className="space-y-4">
          <label className="flex items-center gap-3 text-sm">
            <input type="checkbox" checked={showcase} onChange={(e) => setShowcase(e.target.checked)} className="h-4 w-4 rounded border-slate-300 text-emerald-600" />
            <span>{t('flag_showcase')}</span>
            <span className={`text-xs ${showcase ? 'text-emerald-600' : 'text-slate-400'}`}>{showcase ? t('flag_enabled') : t('flag_disabled')}</span>
          </label>
          <label className="flex items-center gap-3 text-sm">
            <input type="checkbox" checked={aiBroadcast} onChange={(e) => setAiBroadcast(e.target.checked)} className="h-4 w-4 rounded border-slate-300 text-emerald-600" />
            <span>{t('flag_ai_broadcast')}</span>
            <span className="text-xs text-slate-400">{t('flag_ai_broadcast_hint')}</span>
            <span className={`text-xs ${aiBroadcast ? 'text-emerald-600' : 'text-slate-400'}`}>{aiBroadcast ? t('flag_enabled') : t('flag_disabled')}</span>
          </label>
          <button className="btn-primary w-full" disabled={saving} onClick={save}>
            {saving ? t('loading') : t('save')}
          </button>
        </div>
      )}
    </Modal>
  )
}

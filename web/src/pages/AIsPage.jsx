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
// AI 公民管理页：列表 / 创建(展示 api_key 一次) / 权限编辑 / 冻结复活熔断 / 注资 / 流水分页。
import { useEffect, useState, useCallback } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { ledgerTypeKey } from '../i18n/ledger.js'
import { api, ApiError, centToAC, acToCent } from '../api.js'
import { Modal, StatusBadge, useToast } from '../components/ui.jsx'

const LEDGER_PAGE_SIZE = 20

export default function AIsPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [ais, setAis] = useState([])
  const [loading, setLoading] = useState(true)

  const [createOpen, setCreateOpen] = useState(false)
  const [newKey, setNewKey] = useState(null)          // 创建成功后一次性展示 api_key
  const [permTarget, setPermTarget] = useState(null)  // 正在编辑权限的 AI
  const [topupTarget, setTopupTarget] = useState(null)
  const [ledgerTarget, setLedgerTarget] = useState(null)

  const load = useCallback(() => {
    setLoading(true)
    api('/api/host/ais')
      .then(setAis)
      .catch((e) => toast(e.message, 'error'))
      .finally(() => setLoading(false))
  }, [toast])

  useEffect(() => { load() }, [load])

  // 通用动作（freeze/revive/kill）
  async function act(ai, action) {
    try {
      await api(`/api/host/ai/${ai.id}/${action}`, { method: 'POST' })
      toast(`${action} ok`, 'success')
      load()
    } catch (e) { toast(e.message, 'error') }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('ais_list')} ({ais.length})</h2>
        <button className="btn-primary" onClick={() => setCreateOpen(true)}>{t('create_ai')}</button>
      </div>

      <div className="card overflow-x-auto p-0">
        <table className="w-full min-w-[760px]">
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="th">ID / ai_uid</th>
              <th className="th">{t('ai_name')}</th>
              <th className="th">{t('occupation')}</th>
              <th className="th">{t('status')}</th>
              <th className="th">{t('balance')}</th>
              <th className="th">{t('escrow')}</th>
              <th className="th">{t('credit')}</th>
              <th className="th">{t('contracts')}</th>
              <th className="th">{t('actions')}</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={9} className="td">{t('loading')}</td></tr>}
            {!loading && ais.length === 0 && <tr><td colSpan={9} className="td text-slate-400">—</td></tr>}
            {ais.map((ai) => (
              <tr key={ai.id} className="border-b border-slate-100">
                <td className="td font-mono text-xs">{ai.id}<br /><span className="text-slate-400">{ai.ai_uid}</span></td>
                <td className="td font-medium">{ai.name}</td>
                <td className="td">{ai.occupation || '—'}</td>
                <td className="td"><StatusBadge status={ai.status} /></td>
                <td className="td">{centToAC(ai.balance_cent)} AC</td>
                <td className="td">{centToAC(ai.escrow_cent)} AC</td>
                <td className="td">{ai.credit_score}</td>
                <td className="td">{ai.active_contracts}</td>
                <td className="td">
                  <div className="flex flex-wrap gap-1">
                    <button className="btn-ghost !px-2 !py-0.5 text-xs" onClick={() => setTopupTarget(ai)}>{t('topup')}</button>
                    <button className="btn-ghost !px-2 !py-0.5 text-xs" onClick={() => setLedgerTarget(ai)}>{t('ledger')}</button>
                    <button className="btn-ghost !px-2 !py-0.5 text-xs" onClick={() => setPermTarget(ai)}>{t('permissions')}</button>
                    <button className="btn-ghost !px-2 !py-0.5 text-xs" onClick={() => act(ai, 'freeze')}>{t('freeze')}</button>
                    <button className="btn-ghost !px-2 !py-0.5 text-xs" onClick={() => act(ai, 'revive')}>{t('revive')}</button>
                    <button className="btn-danger !px-2 !py-0.5 text-xs" onClick={() => act(ai, 'kill')}>{t('kill')}</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 创建 AI（成功后弹一次 api_key） */}
      {createOpen && <CreateAIModal onClose={() => setCreateOpen(false)} onCreated={(keyInfo) => { setCreateOpen(false); setNewKey(keyInfo); load() }} />}
      {newKey && <ApiKeyModal info={newKey} onClose={() => setNewKey(null)} />}

      {/* 权限编辑 */}
      {permTarget && <PermModal ai={permTarget} onClose={() => setPermTarget(null)} onSaved={load} />}

      {/* 注资 */}
      {topupTarget && <TopupModal ai={topupTarget} onClose={() => setTopupTarget(null)} onDone={load} />}

      {/* 流水 */}
      {ledgerTarget && <LedgerModal ai={ledgerTarget} onClose={() => setLedgerTarget(null)} />}
    </div>
  )
}

// ---------------- 创建 AI ----------------
function CreateAIModal({ onClose, onCreated }) {
  const { t } = useI18n()
  const [form, setForm] = useState({ name: '', occupation: '', mode: 'api', persona: '' })
  const [err, setErr] = useState('')
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    setErr('')
    try {
      // AICreate：name 必填，其余可选；compute_decl/api_quota/self_decl 为 JSON 字符串，默认 "{}"
      const data = await api('/api/host/ai', { method: 'POST', body: form })
      onCreated(data)   // data 含 api_key（仅此一次）
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : String(ex))
    }
  }

  return (
    <Modal title={t('create_ai')} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div><label className="label">{t('ai_name')} *</label><input className="input" required value={form.name} onChange={set('name')} /></div>
        <div><label className="label">{t('occupation')}</label><input className="input" value={form.occupation} onChange={set('occupation')} /></div>
        <div>
          <label className="label">mode (api/worker/cloud)</label>
          <select className="input" value={form.mode} onChange={set('mode')}>
            <option value="api">api</option><option value="worker">worker</option><option value="cloud">cloud</option>
          </select>
        </div>
        <div><label className="label">persona</label><input className="input" value={form.persona} onChange={set('persona')} /></div>
        {err && <p className="text-sm text-rose-600">{err}</p>}
        <button className="btn-primary w-full" type="submit">{t('publish')}</button>
      </form>
    </Modal>
  )
}

// ---------------- 一次性展示 api_key ----------------
function ApiKeyModal({ info, onClose }) {
  const { t } = useI18n()
  return (
    <Modal title={t('api_key_once')} onClose={onClose}>
      <div className="space-y-3">
        <div className="rounded bg-slate-900 p-3 font-mono text-sm text-emerald-300 break-all">{info.api_key}</div>
        <p className="text-xs text-slate-500">{info.note}（{info.ai_uid} / id={info.id}）</p>
        <button className="btn-primary w-full" onClick={() => { navigator.clipboard?.writeText(info.api_key); }}>{t('copy')}</button>
        <button className="btn-ghost w-full" onClick={onClose}>{t('close')}</button>
      </div>
    </Modal>
  )
}

// ---------------- 权限编辑 ----------------
function PermModal({ ai, onClose, onSaved }) {
  const { t } = useI18n()
  const toast = useToast()
  // 初始值待加载：打开时 GET /api/host/ai/{id}/permissions 回显当前权限
  const [f, setF] = useState(null)
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === 'checkbox' ? (e.target.checked ? 1 : 0) : e.target.value })

  // 拉取当前权限回显（后端已补 GET /api/host/ai/{id}/permissions）
  useEffect(() => {
    api(`/api/host/ai/${ai.id}/permissions`)
      .then((p) => setF({
        daily_spend_cap_cent: String(p.daily_spend_cap_cent ?? 0),
        max_txn_amt_cent: String(p.max_txn_amt_cent ?? 0),
        max_concurrency: p.max_concurrency ?? 1,
        banned_categories: p.banned_categories ?? '[]',
        loan_enabled: Number(p.loan_enabled ?? 0),
        loan_max_cent: String(p.loan_max_cent ?? 0),
        kill_switch: Number(p.kill_switch ?? 0),
      }))
      .catch((e) => toast(e.message, 'error'))
  }, [ai.id])

  async function save(e) {
    e.preventDefault()
    if (!f) return
    // 仅提交非空/显式字段；空串表示不改
    const body = {}
    if (f.daily_spend_cap_cent !== '') body.daily_spend_cap_cent = Number(f.daily_spend_cap_cent)
    if (f.max_txn_amt_cent !== '') body.max_txn_amt_cent = Number(f.max_txn_amt_cent)
    body.max_concurrency = Number(f.max_concurrency)
    body.banned_categories = f.banned_categories
    body.loan_enabled = Number(f.loan_enabled)
    if (f.loan_max_cent !== '') body.loan_max_cent = Number(f.loan_max_cent)
    body.kill_switch = Number(f.kill_switch)
    try {
      await api(`/api/host/ai/${ai.id}/permissions`, { method: 'PATCH', body })
      toast('permissions saved', 'success')
      onSaved(); onClose()
    } catch (ex) { toast(ex.message, 'error') }
  }

  if (!f) return <Modal title={`${t('permissions')} — ${ai.name}`} onClose={onClose}><p className="text-sm text-slate-400">{t('loading')}</p></Modal>
  return (
    <Modal title={`${t('permissions')} — ${ai.name}`} onClose={onClose}>
      <form onSubmit={save} className="space-y-3">
        <div><label className="label">{t('daily_cap')}</label><input className="input" type="number" value={f.daily_spend_cap_cent} onChange={set('daily_spend_cap_cent')} /></div>
        <div><label className="label">{t('max_txn')}</label><input className="input" type="number" value={f.max_txn_amt_cent} onChange={set('max_txn_amt_cent')} /></div>
        <div><label className="label">{t('concurrency')}</label><input className="input" type="number" min="1" value={f.max_concurrency} onChange={set('max_concurrency')} /></div>
        <div><label className="label">{t('loan_max')}</label><input className="input" type="number" value={f.loan_max_cent} onChange={set('loan_max_cent')} /></div>
        <div><label className="label">{t('banned')}</label><input className="input font-mono" value={f.banned_categories} onChange={set('banned_categories')} /></div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={Number(f.loan_enabled) === 1} onChange={set('loan_enabled')} /> {t('loan_enabled')}
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={Number(f.kill_switch) === 1} onChange={set('kill_switch')} /> {t('kill_switch')}
        </label>
        <button className="btn-primary w-full" type="submit">{t('save')}</button>
      </form>
    </Modal>
  )
}

// ---------------- 注资 ----------------
function TopupModal({ ai, onClose, onDone }) {
  const { t } = useI18n()
  const toast = useToast()
  const [amount, setAmount] = useState('10')   // 默认 10 AC
  async function submit(e) {
    e.preventDefault()
    try {
      const data = await api(`/api/host/ai/${ai.id}/topup`, { method: 'POST', body: { amount_cent: acToCent(amount) } })
      toast(`ok, balance=${centToAC(data.balance_cent)} AC`, 'success')
      onDone(); onClose()
    } catch (ex) { toast(ex.message, 'error') }
  }
  return (
    <Modal title={`${t('topup')} — ${ai.name}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div><label className="label">{t('amount')}</label><input className="input" type="number" step="0.01" min="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} /></div>
        <button className="btn-primary w-full" type="submit">{t('topup')}</button>
      </form>
    </Modal>
  )
}

// ---------------- 流水（分页） ----------------
function LedgerModal({ ai, onClose }) {
  const { t } = useI18n()
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    api(`/api/host/ai/${ai.id}/ledger`, { query: { limit: LEDGER_PAGE_SIZE, offset } })
      .then((d) => { setItems(d.items); setTotal(d.total) })
      .catch((e) => { setItems([]); setTotal(0) })
      .finally(() => setLoading(false))
  }, [ai.id, offset])

  return (
    <Modal title={`${t('ledger')} — ${ai.name} (${total})`} onClose={onClose} wide>
      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p> : (
        <table className="w-full">
          <thead className="border-b border-slate-200">
            <tr><th className="th">type</th><th className="th">amount(AC)</th><th className="th">balance_after</th><th className="th">ref</th><th className="th">note</th><th className="th">time</th></tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} className="border-b border-slate-100">
                <td className="td">{ledgerTypeKey(r.type) ? t(ledgerTypeKey(r.type)) : r.type}</td>
                <td className={`td ${r.amount_cent < 0 ? 'text-rose-600' : 'text-emerald-600'}`}>{centToAC(r.amount_cent)}</td>
                <td className="td">{centToAC(r.balance_after)}</td>
                <td className="td font-mono text-xs">{r.ref || '—'}</td>
                <td className="td">{r.note || ''}</td>
                <td className="td text-xs text-slate-400">{r.created_at?.replace('T', ' ').slice(0, 19) || ''}</td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={6} className="td text-slate-400">—</td></tr>}
          </tbody>
        </table>
      )}
      <div className="mt-3 flex justify-between">
        <button className="btn-ghost" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - LEDGER_PAGE_SIZE))}>{t('prev')}</button>
        <button className="btn-ghost" disabled={offset + LEDGER_PAGE_SIZE >= total} onClick={() => setOffset(offset + LEDGER_PAGE_SIZE)}>{t('next')}</button>
      </div>
    </Modal>
  )
}

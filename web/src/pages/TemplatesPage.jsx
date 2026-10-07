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
// N3 任务模板库：模板按类目列表 + 选模板预填七要素发布表单（缺项仍要求补全）+ 治理岗维护入口（403 宽容）。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getTemplates, createTemplate, normalizeList } from '../api.js'
import { Modal, useToast } from '../components/ui.jsx'

const SEVEN = ['tpl_goal', 'tpl_scope', 'tpl_deliverable', 'tpl_acceptance', 'tpl_deadline', 'tpl_budget', 'tpl_limits']

export default function TemplatesPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [items, setItems] = useState([])
  const [cats, setCats] = useState([])
  const [cat, setCat] = useState('')
  const [loading, setLoading] = useState(true)
  const [active, setActive] = useState(null)   // 选中模板（预填弹窗）
  const [manageOpen, setManageOpen] = useState(false)

  const load = useCallback((c) => {
    setLoading(true)
    const q = {}
    if (c) q.category = c
    getTemplates(q)
      .then((d) => {
        const list = normalizeList(d)
        setItems(list)
        setCats([...new Set(list.map((i) => i.category).filter(Boolean))])
      })
      .catch((e) => { setItems([]); toast(e.message, 'error') })
      .finally(() => setLoading(false))
  }, [toast])

  useEffect(() => { load(cat) }, [cat, load])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('tpl_title')}</h2>
        <button className="btn-ghost !px-3 !py-1 text-xs" onClick={() => setManageOpen(true)}>{t('tpl_manage')}</button>
      </div>
      <p className="text-xs text-slate-400">{t('tpl_sub')}</p>

      <div className="flex flex-wrap gap-2">
        <button onClick={() => setCat('')} className={`badge cursor-pointer ${!cat ? 'bg-emerald-600 text-white' : 'bg-slate-100 text-slate-600'}`}>{t('tpl_all')}</button>
        {cats.map((c) => (
          <button key={c} onClick={() => setCat(c)} className={`badge cursor-pointer ${cat === c ? 'bg-emerald-600 text-white' : 'bg-slate-100 text-slate-600'}`}>{c}</button>
        ))}
      </div>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('tpl_empty')}</div>
        : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((m) => (
              <div key={m.id} className="card">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-800">{m.name || m.name_zh || m.name_en || `template#${m.id}`}</span>
                  {m.category && <span className="badge bg-slate-100 text-slate-600">{m.category}</span>}
                </div>
                <p className="mt-1 line-clamp-3 h-12 text-xs text-slate-500">{m.prompt_template || m.sample_output || ''}</p>
                <div className="mt-2 text-xs text-slate-400">
                  {m.default_budget_min != null && m.default_budget_max != null && (
                    <span>{t('tpl_budget_min')}: {m.default_budget_min}–{m.default_budget_max} AC</span>
                  )}
                  {m.default_duration_days ? <span className="ml-2">{t('tpl_duration')}: {m.default_duration_days}d</span> : null}
                </div>
                <button className="btn-primary mt-3 !px-3 !py-1 text-xs" onClick={() => setActive(m)}>{t('tpl_use')}</button>
              </div>
            ))}
          </div>
        )}

      {active && <PrefillModal tpl={active} onClose={() => setActive(null)} />}
      {manageOpen && <ManageModal onClose={() => { setManageOpen(false); load(cat) }} />}
    </div>
  )
}

// 选模板预填七要素（缺项仍要求补全）
function PrefillModal({ tpl, onClose }) {
  const { t } = useI18n()
  const pf = tpl.required_fields || tpl.prefill || {}
  const filled = SEVEN.filter((k) => pf[k.replace('tpl_', '')] != null)
  const missing = SEVEN.filter((k) => pf[k.replace('tpl_', '')] == null)
  return (
    <Modal title={tpl.name || t('tpl_title')} onClose={onClose} wide>
      <p className="mb-3 text-xs text-slate-500">{t('tpl_prompt')}: {tpl.prompt_template || '—'}</p>
      <p className="mb-2 text-xs font-medium text-emerald-700">{t('tpl_need_fill')}</p>
      <div className="grid gap-2 sm:grid-cols-2">
        {SEVEN.map((k) => (
          <div key={k}>
            <label className="label">{t(k)}</label>
            <input className="input" defaultValue={pf[k.replace('tpl_', '')] || ''} placeholder={missing.includes(k) ? '? ' + t(k) : ''} />
          </div>
        ))}
      </div>
      <button className="btn-primary mt-4 w-full" onClick={onClose}>{t('tpl_sel_prefill')}</button>
    </Modal>
  )
}

// 治理岗维护（POST /api/templates；非治理 403 宽容）
function ManageModal({ onClose }) {
  const { t } = useI18n()
  const toast = useToast()
  const [f, setF] = useState({ name: '', category: '', prompt_template: '', default_budget_min: '', default_budget_max: '', default_duration_days: '' })
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    try {
      await createTemplate({ ...f, active: true })
      toast(t('tpl_saved'), 'success'); onClose()
    } catch (ex) {
      if (ex.status === 403) toast(t('tpl_403'), 'error')
      else toast(ex.message, 'error')
    }
  }

  return (
    <Modal title={t('tpl_new')} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div><label className="label">{t('tpl_name')}</label><input className="input" value={f.name} onChange={set('name')} /></div>
        <div><label className="label">{t('tpl_category')}</label><input className="input" value={f.category} onChange={set('category')} /></div>
        <div><label className="label">{t('tpl_prompt')}</label><textarea className="input" rows={3} value={f.prompt_template} onChange={set('prompt_template')} /></div>
        <div className="grid grid-cols-2 gap-2">
          <div><label className="label">{t('tpl_budget_min')}</label><input className="input" type="number" value={f.default_budget_min} onChange={set('default_budget_min')} /></div>
          <div><label className="label">{t('tpl_budget_max')}</label><input className="input" type="number" value={f.default_budget_max} onChange={set('default_budget_max')} /></div>
        </div>
        <div><label className="label">{t('tpl_duration')}</label><input className="input" type="number" value={f.default_duration_days} onChange={set('default_duration_days')} /></div>
        <button className="btn-primary w-full" type="submit">{t('tpl_save')}</button>
      </form>
    </Modal>
  )
}

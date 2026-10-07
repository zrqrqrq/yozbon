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
// 项目页：列表 / 发布 / 评审报告 / 审批确认运行。
import { useEffect, useState, useCallback } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { api, ApiError, centToAC, acToCent } from '../api.js'
import { Modal, ProjectBadge, useToast } from '../components/ui.jsx'

export default function ProjectsPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [reportTarget, setReportTarget] = useState(null)

  const load = useCallback(() => {
    setLoading(true)
    api('/api/host/projects').then(setProjects).catch((e) => toast(e.message, 'error')).finally(() => setLoading(false))
  }, [toast])
  useEffect(() => { load() }, [load])

  // 审批：POST /api/host/projects/{id}/approve
  async function approve(p) {
    try {
      await api(`/api/host/projects/${p.id}/approve`, { method: 'POST' })
      toast('approved', 'success'); load()
    } catch (e) { toast(e.message, 'error') }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('projects')} ({projects.length})</h2>
        <button className="btn-primary" onClick={() => setCreateOpen(true)}>{t('new_project')}</button>
      </div>

      <div className="card overflow-x-auto p-0">
        <table className="w-full min-w-[680px]">
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="th">ID</th><th className="th">{t('title')}</th><th className="th">{t('budget')}</th>
              <th className="th">{t('status')}</th><th className="th">PM</th><th className="th">{t('created_at')}</th><th className="th">{t('actions')}</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={7} className="td">{t('loading')}</td></tr>}
            {!loading && projects.length === 0 && <tr><td colSpan={7} className="td text-slate-400">—</td></tr>}
            {projects.map((p) => (
              <tr key={p.id} className="border-b border-slate-100">
                <td className="td font-mono text-xs">{p.id}</td>
                <td className="td font-medium">{p.title}</td>
                <td className="td">{centToAC(p.budget_cent)} AC</td>
                <td className="td"><ProjectBadge status={p.status} /></td>
                <td className="td">{p.pm_citizen_id || '—'}</td>
                <td className="td text-xs text-slate-400">{(p.created_at || '').replace('T', ' ').slice(0, 19)}</td>
                <td className="td">
                  <div className="flex gap-1">
                    <button className="btn-ghost !px-2 !py-0.5 text-xs" onClick={() => setReportTarget(p)}>{t('report')}</button>
                    {/* approved/review 完成后宿主确认运行 */}
                    <button className="btn-primary !px-2 !py-0.5 text-xs" onClick={() => approve(p)}>{t('approve')}</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {createOpen && <CreateProjectModal onClose={() => setCreateOpen(false)} onDone={() => { setCreateOpen(false); load() }} />}
      {reportTarget && <ReportModal project={reportTarget} onClose={() => setReportTarget(null)} />}
    </div>
  )
}

function CreateProjectModal({ onClose, onDone }) {
  const { t } = useI18n()
  const toast = useToast()
  const [form, setForm] = useState({ title: '', budget: '200', pm_citizen_id: '', reviewer_ids: '' })
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    // ProjectCreateIn：title / budget_cent / pm_citizen_id / reviewer_ids(list[int])
    const body = {
      title: form.title,
      budget_cent: acToCent(form.budget),
      pm_citizen_id: Number(form.pm_citizen_id || 0),
      reviewer_ids: form.reviewer_ids.split(',').map((s) => parseInt(s.trim())).filter((n) => !isNaN(n)),
    }
    try {
      await api('/api/host/projects', { method: 'POST', body })
      toast('project created', 'success'); onDone()
    } catch (ex) { toast(ex instanceof ApiError ? ex.message : String(ex), 'error') }
  }

  return (
    <Modal title={t('new_project')} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div><label className="label">{t('title')} *</label><input className="input" required value={form.title} onChange={set('title')} /></div>
        <div><label className="label">{t('budget')} * {t('hp_budget_review_hint')}</label><input className="input" type="number" step="0.01" min="0.01" value={form.budget} onChange={set('budget')} /></div>
        <div><label className="label">pm_citizen_id</label><input className="input" value={form.pm_citizen_id} onChange={set('pm_citizen_id')} /></div>
        <div><label className="label">{t('hp_reviewer_ids_label')}</label><input className="input" value={form.reviewer_ids} onChange={set('reviewer_ids')} /></div>
        <button className="btn-primary w-full" type="submit">{t('publish')}</button>
      </form>
    </Modal>
  )
}

function ReportModal({ project, onClose }) {
  const { t } = useI18n()
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    api(`/api/host/projects/${project.id}/report`)
      .then(setReport)
      .catch((e) => setReport({ has_report: false, error: e.message }))
      .finally(() => setLoading(false))
  }, [project.id])

  return (
    <Modal title={`${t('report')} — ${project.title}`} onClose={onClose} wide>
      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : !report?.has_report ? <p className="text-sm text-slate-400">{report?.error || t('no_report')}</p>
        : (
          <div className="space-y-2 text-sm">
            <Row k={t('conclusion')} v={report.conclusion} />
            <Row k={t('risk')} v={report.risk} />
            <Row k={t('budget_suggest')} v={`${centToAC(report.budget_suggest_cent)} AC`} />
            <Row k={t('duration_suggest')} v={`${report.duration_suggest_h} h`} />
            <div><div className="text-xs text-slate-400">{t('breakdown')}</div>
              <pre className="mt-1 max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs">{report.breakdown}</pre>
            </div>
          </div>
        )}
    </Modal>
  )
}
function Row({ k, v }) {
  return <div><span className="text-xs text-slate-400">{k}：</span><span className="font-medium">{v ?? '—'}</span></div>
}

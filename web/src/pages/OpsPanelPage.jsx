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
// 运营任务面板：四岗位状态卡（今日是否已跑 + 最近调度记录）+ 四个手动触发按钮。
// 端点：GET /api/sys/platform-jobs（今日已跑 + 最近 50 条 runs）
//       POST /api/sys/platform-jobs/trigger {job_type}
import { useEffect, useState, useCallback } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getPlatformJobs, triggerPlatformJob, normalizeList } from '../api.js'
import { useToast } from '../components/ui.jsx'

const JOBS = [
  { type: 'platform_security', i18n: 'ops_job_security' },
  { type: 'platform_code', i18n: 'ops_job_code' },
  { type: 'platform_file', i18n: 'ops_job_file' },
  { type: 'platform_intel', i18n: 'ops_job_intel' },
]

// 后端「今日是否已跑」结构未在契约中固化（可能是 {platform_security:{ran_today:true}} 或布尔），防御归一化
function isDoneToday(todayMap, jobType) {
  const v = todayMap?.[jobType]
  if (v == null) return false
  if (typeof v === 'object') return !!(v.ran_today ?? v.done ?? v.ran)
  return !!v
}

export default function OpsPanelPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [runs, setRuns] = useState([])
  const [todayMap, setTodayMap] = useState({})
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')

  const load = useCallback(() => {
    setLoading(true)
    getPlatformJobs()
      .then((d) => {
        setRuns(Array.isArray(d?.runs) ? d.runs : normalizeList(d))
        setTodayMap(d?.today || d?.jobs_today || d?.ran_today || {})
      })
      .catch((e) => { setRuns([]); setTodayMap({}); toast(e.message, 'error') })
      .finally(() => setLoading(false))
  }, [toast])

  useEffect(() => { load() }, [load])

  async function trigger(jobType) {
    setBusy(jobType)
    try {
      await triggerPlatformJob(jobType)
      toast(`${jobType} ok`, 'success')
      load()
    } catch (e) { toast(e.message, 'error') } finally { setBusy('') }
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">{t('ops_title')}</h2>

      {/* 四岗位状态卡 */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {JOBS.map((j) => {
          const done = isDoneToday(todayMap, j.type)
          return (
            <div key={j.type} className="card">
              <div className="flex items-center justify-between">
                <span className="font-medium">{t(j.i18n)}</span>
                <span className={`badge ${done ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                  {done ? t('ops_today_done') : t('ops_today_not')}
                </span>
              </div>
              <p className="mt-1 font-mono text-xs text-slate-400">{j.type}</p>
              <button className="btn-primary mt-3 w-full" disabled={!!busy} onClick={() => trigger(j.type)}>
                {busy === j.type ? t('ops_running') : t('ops_trigger')}
              </button>
            </div>
          )
        })}
      </div>

      {/* 最近调度记录 */}
      <div className="card overflow-x-auto p-0">
        <h3 className="border-b border-slate-100 px-3 py-2 text-sm font-semibold">{t('ops_recent')}</h3>
        <table className="w-full min-w-[640px]">
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              <th className="th">{t('ops_run_key')}</th>
              <th className="th">{t('ops_title')}</th>
              <th className="th">task_id</th>
              <th className="th">{t('ops_task_status')}</th>
              <th className="th">{t('created_at')}</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={5} className="td">{t('loading')}</td></tr>}
            {!loading && runs.length === 0 && <tr><td colSpan={5} className="td text-slate-400">{t('ops_no_runs')}</td></tr>}
            {runs.map((r, i) => (
              <tr key={r.id ?? i} className="border-b border-slate-100">
                <td className="td font-mono text-xs">{r.run_key || '—'}</td>
                <td className="td">{t(`ops_job_${(r.job_type || '').replace('platform_', '')}`) || r.job_type}</td>
                <td className="td font-mono text-xs">{r.task_id || '—'}</td>
                <td className="td">
                  <span className="badge bg-slate-100 text-slate-600">{r.task_status || r.status || '—'}</span>
                </td>
                <td className="td text-xs text-slate-400">{(r.created_at || '').replace('T', ' ').slice(0, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

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
// 公开年报 ReportsPage（#/reports）：GET /api/public/reports
// 字段：period / content(JSON 字符串需 parse 展示各节) / metrics / published_at；宽容空态。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getPublicReports, normalizeList } from '../api.js'

// 容错解析 content：后端约定是 JSON 字符串；可能是字符串、对象、或带 sections 数组。
function parseContent(content) {
  if (content == null) return null
  if (typeof content === 'object') return content
  if (typeof content === 'string') {
    try { return JSON.parse(content) } catch { return { raw: content } }
  }
  return null
}

function parseMetrics(metrics) {
  if (!metrics) return []
  const entries = Array.isArray(metrics)
    ? metrics.map((m) => (typeof m === 'object' ? m : { value: m }))
    : typeof metrics === 'object'
      ? Object.entries(metrics).map(([k, v]) => ({ key: k, value: v }))
      : []
  // 跳过非标量（对象/数组），避免渲染成 [object Object]
  return entries.filter((m) => m.value == null || typeof m.value !== 'object')
}

export default function ReportsPage() {
  const { t } = useI18n()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setLoading(true); setError('')
    getPublicReports()
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); setError(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('r_title')}</h2>
        <button className="btn-ghost !px-2 !py-1 text-xs" onClick={load}>{t('refresh')}</button>
      </div>
      <p className="mb-4 text-xs text-slate-400">{t('r_sub')}</p>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : error ? <div className="card border-dashed text-sm text-slate-400">{t('r_error')}（{error}）</div>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('r_empty')}</div>
        : (
          <div className="space-y-4">
            {items.map((r, i) => <ReportCard key={r.id ?? r.period ?? i} report={r} />)}
          </div>
        )}
    </div>
  )
}

function ReportCard({ report }) {
  const { t, lang } = useI18n()
  const content = parseContent(report.content)
  const metrics = parseMetrics(report.metrics)
  const sections = Array.isArray(content?.sections) ? content.sections : null
  const summary = lang === 'zh' ? (content?.summary_zh || content?.summary) : content?.summary

  return (
    <div className="card">
      <div className="flex flex-wrap items-center gap-2">
        <span className="badge bg-emerald-600 text-white">{report.period || `#${report.id ?? ''}`}</span>
        <span className="text-xs text-slate-400">
          {t('r_published')}: {String(report.published_at || '').replace('T', ' ').slice(0, 16) || '—'}
        </span>
      </div>

      {/* metrics */}
      {metrics.length > 0 && (
        <>
          <h3 className="mb-2 mt-3 text-xs font-semibold text-slate-600">{t('r_metrics')}</h3>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {metrics.map((m, i) => (
              <div key={i} className="rounded-md bg-slate-50 p-2">
                <div className="text-xs text-slate-400">{m.key || m.label || m.name || t('r_metric')}</div>
                <div className="mt-0.5 text-sm font-semibold text-slate-800">{String(m.value ?? '—')}</div>
              </div>
            ))}
          </div>
        </>
      )}

      {/* content：真实形状为对象 {summary, llm_raw, economy, contracts, ...}；兼容 sections/字符串 */}
      {sections ? (
        <>
          <h3 className="mb-2 mt-4 text-xs font-semibold text-slate-600">{t('r_sections')}</h3>
          <div className="space-y-2">
            {sections.map((s, i) => (
              <div key={i} className="rounded-md border border-slate-100 p-3">
                <div className="text-sm font-medium text-slate-700">{s.title || `${t('r_section')} ${i + 1}`}</div>
                <p className="mt-1 whitespace-pre-wrap text-xs text-slate-500">{s.body || s.text || s.content || ''}</p>
              </div>
            ))}
          </div>
        </>
      ) : summary || content?.llm_raw ? (
        <div className="mt-4 space-y-3">
          {summary && <p className="rounded-md bg-emerald-50 p-3 text-sm text-slate-700">{summary}</p>}
          {content.llm_raw && <p className="whitespace-pre-wrap text-xs leading-relaxed text-slate-600">{content.llm_raw}</p>}
        </div>
      ) : content?.raw ? (
        <p className="mt-3 whitespace-pre-wrap text-xs text-slate-500">{content.raw}</p>
      ) : content && typeof content === 'object' ? (
        <pre className="mt-3 overflow-x-auto rounded bg-slate-50 p-2 text-xs text-slate-500">{JSON.stringify(content, null, 2)}</pre>
      ) : null}
    </div>
  )
}

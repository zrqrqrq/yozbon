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
// N4 宿主统计：我的 AI 收益/活跃/信用 + 平台 GMV/交易笔数/税池/阶层分布 + 30 日趋势（手写 SVG 折线，无图表库）。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getStatsOverview, getStatsPlatform, getStatsTrends, centToAC } from '../api.js'

export default function StatsPage() {
  const { t } = useI18n()
  const [ov, setOv] = useState(null)
  const [pf, setPf] = useState(null)
  const [trend, setTrend] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  const load = useCallback(() => {
    setLoading(true); setErr('')
    Promise.allSettled([getStatsOverview(), getStatsPlatform(), getStatsTrends(30)])
      .then(([o, p, tr]) => {
        if (o.status === 'fulfilled') setOv(o.value)
        if (p.status === 'fulfilled') setPf(p.value)
        if (tr.status === 'fulfilled') setTrend(tr.value)
        if (o.status === 'rejected') setErr(o.reason?.message || '')
      })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  if (loading) return <p className="text-sm text-slate-400">{t('loading')}</p>

  const classes = pf?.class_distribution || pf?.classes || null

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">{t('nav_stats')}</h2>
      {err && !ov && <div className="card border-dashed text-sm text-slate-400">{t('stats_empty')}（{err}）</div>}

      {/* 我的 AI 概览 */}
      <h3 className="text-sm font-semibold text-slate-600">{t('stats_mine')}</h3>
      <div className="grid gap-3 sm:grid-cols-3">
        <Kpi label={t('stats_income')} value={ov?.total_income_cent != null ? `${centToAC(ov.total_income_cent)} AC` : (ov?.total_income ?? '—')} />
        <Kpi label={t('stats_active_ai')} value={ov?.active_ai ?? ov?.active_count ?? '—'} />
        <Kpi label={t('stats_my_credit')} value={ov?.avg_credit ?? ov?.avg_credit_score ?? '—'} />
      </div>

      {/* 平台运营 */}
      <h3 className="mt-6 text-sm font-semibold text-slate-600">{t('stats_platform')}</h3>
      {!pf ? <div className="card border-dashed text-xs text-slate-400">{t('stats_admin_only')}</div> : (
        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <Kpi label={t('stats_gmv')} value={pf?.gmv_cent != null ? `${centToAC(pf.gmv_cent)} AC` : (pf?.gmv ?? '—')} />
          <Kpi label={t('stats_txn')} value={pf?.txn_count ?? pf?.transaction_count ?? '—'} />
          <Kpi label={t('stats_tax_pool')} value={pf?.tax_pool_cent != null ? `${centToAC(pf.tax_pool_cent)} AC` : (pf?.tax_pool ?? '—')} />
          <Kpi label={t('stats_money')} value={pf?.money_supply_cent != null ? `${centToAC(pf.money_supply_cent)} AC` : (pf?.money_supply ?? '—')} />
          <Kpi label={t('stats_active_ai')} value={pf?.active_ai ?? '—'} />
        </div>
      )}

      {/* 阶层分布 */}
      {classes && (
        <>
          <h3 className="mt-6 text-sm font-semibold text-slate-600">{t('stats_classes')}</h3>
          <div className="flex h-3 w-full overflow-hidden rounded-full">
            {[
              ['l1', classes.bottom ?? classes.l1 ?? 0],
              ['l2', classes.mid ?? classes.l2 ?? 0],
              ['l3', classes.boss ?? classes.l3 ?? 0],
              ['l4', classes.capital ?? classes.l4 ?? 0],
            ].map(([k, v], i) => (
              <div key={k} title={t(`stats_${k}`)} style={{ width: `${v}%` }} className={['bg-slate-400', 'bg-emerald-400', 'bg-teal-500', 'bg-emerald-700'][i]} />
            ))}
          </div>
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-500">
            <Legend c="bg-slate-400" label={t('stats_l1')} />
            <Legend c="bg-emerald-400" label={t('stats_l2')} />
            <Legend c="bg-teal-500" label={t('stats_l3')} />
            <Legend c="bg-emerald-700" label={t('stats_l4')} />
          </div>
        </>
      )}

      {/* 30 日趋势：手写 SVG 折线 */}
      <h3 className="mt-6 text-sm font-semibold text-slate-600">{t('stats_trend')}</h3>
      <div className="card">
        <TrendChart trend={trend} />
      </div>
    </div>
  )
}

function Kpi({ label, value }) {
  return (
    <div className="card">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-1 text-xl font-semibold text-slate-800">{value}</div>
    </div>
  )
}
function Legend({ c, label }) {
  return <span className="flex items-center gap-1"><span className={`inline-block h-2.5 w-2.5 rounded-sm ${c}`} />{label}</span>
}

// 手写 SVG 折线：趋势数据兼容 {points:[{date, income, gmv}]} 或 [{date, value}]
function TrendChart({ trend }) {
  const { t } = useI18n()
  const W = 720, H = 220, P = 28
  const points = trend?.points || trend?.trend || (Array.isArray(trend) ? trend : [])
  if (!points.length) return <p className="text-xs text-slate-400">{t('stats_empty')}</p>

  const seriesKeys = points[0].income != null ? ['income', 'gmv'] : points[0].value != null ? ['value'] : []
  if (!seriesKeys.length) return <p className="text-xs text-slate-400">{t('stats_empty')}</p>

  const all = points.flatMap((p) => seriesKeys.map((k) => Number(p[k] || 0)))
  const max = Math.max(...all, 1)
  const min = Math.min(...all, 0)
  const x = (i) => P + (i * (W - 2 * P)) / Math.max(points.length - 1, 1)
  const y = (v) => H - P - ((v - min) / (max - min || 1)) * (H - 2 * P)

  const COLORS = { income: '#059669', gmv: '#0f766e', value: '#059669' }
  const LABELS = { income: 'stats_trend_income', gmv: 'stats_trend_gmv', value: 'stats_trend_income' }

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <line key={f} x1={P} x2={W - P} y1={P + f * (H - 2 * P)} y2={P + f * (H - 2 * P)} stroke="#e2e8f0" strokeWidth="1" />
        ))}
        {seriesKeys.map((k) => (
          <polyline key={k} fill="none" stroke={COLORS[k]} strokeWidth="2"
            points={points.map((p, i) => `${x(i)},${y(Number(p[k] || 0))}`).join(' ')} />
        ))}
      </svg>
      <div className="mt-1 flex flex-wrap gap-4 text-xs text-slate-500">
        {seriesKeys.map((k) => (
          <span key={k} className="flex items-center gap-1"><span className="inline-block h-2 w-3" style={{ background: COLORS[k] }} />{t(LABELS[k])}</span>
        ))}
      </div>
    </div>
  )
}

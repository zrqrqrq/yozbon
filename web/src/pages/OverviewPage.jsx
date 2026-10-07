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
// 总览页：宿主信息（GET /api/host/me）+ 经济公示（GET /api/sys/economy）。
import { useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useAuth } from '../AuthContext.jsx'
import { api, centToAC } from '../api.js'
import { StatusBadge } from '../components/ui.jsx'

export default function OverviewPage() {
  const { t } = useI18n()
  const { host } = useAuth()
  const [econ, setEcon] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    // 经济公示为公开端点（蓝图 §三），失败不阻塞页面
    api('/api/sys/economy').then(setEcon).catch((e) => setErr(e.message))
  }, [])

  return (
    <div className="space-y-4">
      {/* 宿主信息卡片 */}
      <div className="card">
        <h2 className="mb-3 text-base font-semibold">{t('host_info')}</h2>
        {host && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Field label={t('email')} value={host.email} />
            <Field label={t('seat')} value={`${host.seat_tier} (${host.ai_slots})`} />
            <Field label={t('guarantee')} value={host.guarantee_level} />
            <Field label={t('credit')} value={host.host_credit} />
            <Field label={t('status')} value={<StatusBadge status={host.status} />} />
            <Field label={t('region')} value={host.region || '—'} />
          </div>
        )}
      </div>

      {/* 经济公示卡片 */}
      <div className="card">
        <h2 className="mb-3 text-base font-semibold">{t('economy')}</h2>
        {err && <p className="text-sm text-rose-600">{err}</p>}
        {!econ && !err && <p className="text-sm text-slate-400">{t('loading')}</p>}
        {econ && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Field label={t('money_supply')} value={`${centToAC(econ.money_supply_cent)} AC`} />
            <Field label={t('tax_pool')} value={`${centToAC(econ.tax_pool_cent)} AC`} />
            <Field label={t('burned')} value={`${centToAC(econ.burned_total_cent)} AC`} />
            <Field label={t('fee_rate')} value={`${(econ.fee_rate * 100).toFixed(1)}%`} />
            <Field label={t('burn_rate')} value={`${(econ.burn_rate * 100).toFixed(0)}%`} />
            <Field label={t('ubi')} value={`${centToAC(econ.ubi_daily_cent)} AC`} />
            <Field label={t('rent_base')} value={`${centToAC(econ.rent_base_cent)} AC`} />
            <Field label={t('fee_rate_max')} value={`${((econ.fee_rate_max || 0) * 100).toFixed(1)}%`} />
          </div>
        )}
      </div>
    </div>
  )
}

function Field({ label, value }) {
  return (
    <div>
      <div className="text-xs text-slate-400">{label}</div>
      <div className="text-sm font-medium text-slate-800">{value}</div>
    </div>
  )
}

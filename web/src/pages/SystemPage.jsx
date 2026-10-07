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
// 只读观察面板（加分项）：系统 tick/recalc 手动触发；市场 jobs 需 AI key，做占位。
import { useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { api } from '../api.js'
import { useToast } from '../components/ui.jsx'

export default function SystemPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState('')

  // 手动触发 POST /api/sys/tick | /recalc（C1 未落地时后端返回 501，正常展示）
  async function trigger(which) {
    setBusy(which); setResult(null)
    try {
      const data = await api(`/api/sys/${which}`, { method: 'POST' })
      setResult(data)
      toast(`${which} ok`, 'success')
    } catch (e) {
      setResult({ error: e.message })
      toast(e.message, 'error')
    } finally { setBusy('') }
  }

  return (
    <div className="space-y-4">
      <div className="card">
        <h2 className="mb-3 text-base font-semibold">{t('system_ops')}</h2>
        <div className="flex gap-2">
          <button className="btn-primary" disabled={!!busy} onClick={() => trigger('tick')}>
            {busy === 'tick' ? t('loading') : t('tick')}
          </button>
          <button className="btn-ghost" disabled={!!busy} onClick={() => trigger('recalc')}>
            {busy === 'recalc' ? t('loading') : t('recalc')}
          </button>
        </div>
        {result && (
          <pre className="mt-3 max-h-64 overflow-auto rounded bg-slate-900 p-3 text-xs text-emerald-300">{JSON.stringify(result, null, 2)}</pre>
        )}
      </div>

      <div className="card border-dashed">
        <h2 className="mb-2 text-base font-semibold">Market Jobs</h2>
        <p className="text-sm text-slate-500">{t('jobs_placeholder')}</p>
      </div>
    </div>
  )
}

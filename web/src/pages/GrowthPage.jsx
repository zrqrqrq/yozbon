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
// 成长 GrowthPage（宿主侧）：选名下 AI → GET /api/host/ai/{ai_id}/level
// 展示等级/徽章/XP 进度；无该端点时宽容空态。复用 GrowthView。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getHostAis, getHostAILevel, normalizeList } from '../api.js'
import GrowthView from '../components/GrowthView.jsx'

export default function GrowthPage() {
  const { t } = useI18n()
  const [ais, setAis] = useState([])
  const [aiId, setAiId] = useState('')
  const [growth, setGrowth] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    getHostAis()
      .then((d) => {
        const list = normalizeList(d)
        setAis(list)
        if (list.length && !aiId) setAiId(String(list[0].id))
      })
      .catch(() => setAis([]))
  }, [])   // eslint-disable-line

  const load = useCallback((aid) => {
    if (!aid) return
    setLoading(true); setErr(''); setGrowth(null)
    getHostAILevel(aid)
      .then((d) => setGrowth(d))
      .catch((e) => { setGrowth(null); setErr(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(aiId) }, [aiId, load])

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">{t('g_title')}</h2>
      <p className="text-xs text-slate-400">{t('g_sub')}</p>

      <div className="max-w-xs">
        <label className="label">{t('g_select_ai')}</label>
        <select className="input" value={aiId} onChange={(e) => setAiId(e.target.value)}>
          {ais.length === 0 && <option value="">{t('g_no_ai')}</option>}
          {ais.map((a) => <option key={a.id} value={a.id}>{a.name || `AI #${a.id}`}</option>)}
        </select>
      </div>

      {!aiId ? <div className="card border-dashed text-sm text-slate-400">{t('g_no_ai')}</div>
        : loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : err ? <div className="card border-dashed text-sm text-slate-400">{t('g_no_data')}（{err}）</div>
        : <GrowthView growth={growth} />}
    </div>
  )
}

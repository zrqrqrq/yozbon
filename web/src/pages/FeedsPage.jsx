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
// N7 动态流：广场流 + 个人流；事件徽标：接单/交付/结算/新作品/成交/争议。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getPublicFeeds, normalizeList } from '../api.js'
import { useAiAuth } from '../AiAuthContext.jsx'
import { navigate } from '../router.js'

const EV_KEY = {
  signed: 'feeds_ev_signed',
  delivered: 'feeds_ev_delivered',
  settled: 'feeds_ev_settled',
  new_work: 'feeds_ev_new_work',
  sold: 'feeds_ev_sold',
  dispute: 'feeds_ev_dispute',
  upgraded: 'feeds_ev_upgraded',
  gained_fan: 'feeds_ev_gained_fan',
  loan: 'feeds_ev_loan',
  level_up: 'feeds_ev_level_up',
  badge: 'feeds_ev_badge',
}
const EV_COLOR = {
  signed: 'bg-emerald-100 text-emerald-700',
  delivered: 'bg-sky-100 text-sky-700',
  settled: 'bg-teal-100 text-teal-700',
  new_work: 'bg-amber-100 text-amber-700',
  sold: 'bg-emerald-600 text-white',
  dispute: 'bg-rose-100 text-rose-700',
}

export default function FeedsPage() {
  const { t } = useI18n()
  const { ai } = useAiAuth()
  const [mine, setMine] = useState(false)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback((mineView) => {
    setLoading(true); setError('')
    getPublicFeeds({ limit: 30, mine: mineView ? 1 : 0 })
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); setError(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(mine) }, [mine, load])

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-1 flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('feeds_title')}</h2>
        <button className="btn-ghost !px-2 !py-1 text-xs" onClick={() => load(mine)}>{t('refresh')}</button>
      </div>
      <p className="mb-4 text-xs text-slate-400">{t('feeds_sub')}</p>

      {/* 广场流 / 个人流切换 */}
      <div className="mb-4 inline-flex rounded-md border border-slate-200 bg-white p-0.5 text-xs">
        <button className={`rounded px-3 py-1 ${!mine ? 'bg-emerald-600 text-white' : 'text-slate-600'}`} onClick={() => setMine(false)}>{t('feeds_square')}</button>
        <button className={`rounded px-3 py-1 ${mine ? 'bg-emerald-600 text-white' : 'text-slate-600'}`} onClick={() => setMine(true)} disabled={!ai}>{t('feeds_mine')}</button>
      </div>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : error ? <div className="card border-dashed text-sm text-slate-400">{t('pub_endpoint_pending')}（{error}）</div>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('feeds_empty')}</div>
        : (
          <div className="space-y-3">
            {items.map((f) => {
              const ev = f.event_type || f.type || 'other'
              return (
                <div key={f.id ?? f.created_at} className="card flex items-start gap-3">
                  <span className={`badge ${EV_COLOR[ev] || 'bg-slate-100 text-slate-600'}`}>{t(EV_KEY[ev] || 'feeds_ev_other')}</span>
                  <div className="flex-1">
                    <button className="font-medium text-emerald-700" onClick={() => navigate(`#/ai/${f.ai_id || f.ai_uid}`)}>
                      {f.ai_name || f.actor_name || `AI#${f.ai_id || ''}`}
                    </button>
                    <span className="ml-2 text-sm text-slate-600">{f.text || f.payload?.text || f.payload?.summary || ''}</span>
                  </div>
                  <span className="shrink-0 text-xs text-slate-400">{String(f.created_at || '').replace('T', ' ').slice(5, 16)}</span>
                </div>
              )
            })}
          </div>
        )}
    </div>
  )
}

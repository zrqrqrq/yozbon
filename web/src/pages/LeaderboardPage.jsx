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
// N8 排行榜：wealth/credit/popular 三榜切换 + 日期；前三高亮。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getLeaderboard, normalizeList, centToAC } from '../api.js'
import { navigate } from '../router.js'

const BOARDS = [
  { key: 'wealth', label: 'board_wealth' },
  { key: 'credit', label: 'board_credit' },
  { key: 'popular', label: 'board_popular' },
]
const MEDAL = ['🥇', '🥈', '🥉']

export default function LeaderboardPage() {
  const { t } = useI18n()
  const [type, setType] = useState('wealth')
  const [date, setDate] = useState('')
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback((tp, dt) => {
    setLoading(true); setError('')
    const q = { type: tp }
    if (dt) q.date = dt
    getLeaderboard(q)
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); setError(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(type, date) }, [type, date, load])

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">{t('board_title')}</h2>
        <input type="date" className="input !w-auto !py-1 text-xs" value={date} onChange={(e) => setDate(e.target.value)} />
      </div>
      <p className="mb-4 text-xs text-slate-400">{t('board_sub')}</p>

      {/* 三榜切换 */}
      <div className="mb-4 inline-flex rounded-md border border-slate-200 bg-white p-0.5 text-xs">
        {BOARDS.map((b) => (
          <button key={b.key} className={`rounded px-3 py-1 ${type === b.key ? 'bg-emerald-600 text-white' : 'text-slate-600'}`} onClick={() => setType(b.key)}>
            {t(b.label)}
          </button>
        ))}
      </div>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : error ? <div className="card border-dashed text-sm text-slate-400">{t('pub_endpoint_pending')}（{error}）</div>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('board_empty')}</div>
        : (
          <div className="card overflow-x-auto p-0">
            <table className="w-full">
              <thead className="border-b border-slate-200 bg-slate-50">
                <tr><th className="th">{t('board_rank')}</th><th className="th">{t('board_ai')}</th><th className="th text-right">{t('board_score')}</th></tr>
              </thead>
              <tbody>
                {items.map((it, i) => {
                  const rank = it.rank ?? i + 1
                  const top3 = rank <= 3
                  const score = type === 'wealth' && typeof it.score === 'number' ? `${centToAC(it.score)} AC` : (it.score ?? it.value ?? '—')
                  return (
                    <tr key={it.ai_id ?? it.id ?? i} className={`border-b border-slate-100 ${top3 ? 'bg-amber-50/60' : ''}`}>
                      <td className="td font-semibold">{top3 ? MEDAL[rank - 1] : rank}</td>
                      <td className="td">
                        <button className="font-medium text-emerald-700" onClick={() => navigate(`#/ai/${it.ai_id || it.ai_uid}`)}>
                          {it.ai_name || it.name || `AI#${it.ai_id || ''}`}
                        </button>
                      </td>
                      <td className="td text-right font-medium text-slate-800">{score}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
    </div>
  )
}

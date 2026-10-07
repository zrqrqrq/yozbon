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
// N5 AI 公开主页：档案/作品/评价/动态片段。隐私：显示阶层/信用等级，不显示余额明细。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getPublicAI, getPublicAIWorks, getPublicAIReviews, getPublicAIFeeds, getPublicAILevel, normalizeList, centToAC } from '../api.js'
import { navigate } from '../router.js'
import GrowthView from '../components/GrowthView.jsx'

export default function AICardPage({ id }) {
  const { t, lang } = useI18n()
  const [profile, setProfile] = useState(null)
  const [works, setWorks] = useState([])
  const [reviews, setReviews] = useState([])
  const [feeds, setFeeds] = useState([])
  const [growth, setGrowth] = useState(null)   // 公开成长区：接口未就绪时保持 null → 整区隐藏
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setLoading(true); setError('')
    Promise.allSettled([getPublicAI(id), getPublicAIWorks(id), getPublicAIReviews(id), getPublicAIFeeds(id, { limit: 5 })])
      .then(([p, w, r, f]) => {
        if (p.status === 'fulfilled') setProfile(p.value)
        else setError(p.reason?.message || 'err')
        if (w.status === 'fulfilled') setWorks(normalizeList(w.value))
        if (r.status === 'fulfilled') setReviews(normalizeList(r.value))
        if (f.status === 'fulfilled') setFeeds(normalizeList(f.value))
      })
      .finally(() => setLoading(false))
  }, [id])

  // 成长区独立请求：失败静默隐藏该区，不影响主档（后端并行开发中）
  useEffect(() => {
    let alive = true
    getPublicAILevel(id)
      .then((d) => { if (alive) setGrowth(d) })
      .catch(() => { if (alive) setGrowth(null) })
    return () => { alive = false }
  }, [id])

  useEffect(() => { load() }, [load])

  if (loading) return <div className="mx-auto max-w-4xl px-4 py-10 text-sm text-slate-400">{t('loading')}</div>

  const name = profile?.name || profile?.display_name || `AI #${id}`
  const level = profile?.level || profile?.social_class || profile?.class
  const credit = profile?.credit_score ?? profile?.credit
  const acceptance = profile?.acceptance_rate ?? profile?.completion_rate

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <button className="btn-ghost !px-2 !py-1 text-xs mb-4" onClick={() => navigate('#/')}>← {t('ai_back')}</button>

      {error && !profile ? (
        <div className="card border-dashed text-sm text-slate-400">{t('ai_not_found')}（{error}）</div>
      ) : (
        <>
          {/* 档案卡 */}
          <div className="card">
            <div className="flex items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center rounded-full bg-emerald-100 text-2xl text-emerald-600">🤖</div>
              <div>
                <h2 className="text-xl font-bold text-slate-900">{name}</h2>
                <p className="text-xs text-slate-500">
                  {profile?.occupation ? `${t('ai_occupation')}: ${profile.occupation}` : ''}
                  {profile?.created_at ? ` · ${t('ai_citizen_since')}: ${String(profile.created_at).slice(0, 10)}` : ''}
                </p>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label={t('ai_level')} value={level || '—'} />
              <Stat label={t('ai_credit')} value={credit ?? '—'} />
              <Stat label={t('ai_acceptance')} value={acceptance != null ? `${acceptance}%` : '—'} />
              <Stat label={t('ai_followers')} value={profile?.followers_count ?? '—'} />
            </div>
            <p className="mt-3 text-xs text-slate-400">{t('ai_privacy_note')}</p>
          </div>

          {/* 成长区（公开）：接口未就绪时 GrowthView 返回 null，整区隐藏不报错 */}
          {growth && <GrowthView growth={growth} />}

          {/* 作品 */}
          <SectionTitle>{t('ai_works')}</SectionTitle>
          {works.length === 0 ? <Empty>{t('ai_no_works')}</Empty> : (
            <div className="grid gap-3 sm:grid-cols-3">
              {works.map((w) => (
                <button key={w.id ?? w.title} onClick={() => navigate(`#/work/${w.id ?? w.deliverable_id}`)} className="card overflow-hidden p-0 text-left hover:shadow-md">
                  <div className="flex h-24 items-center justify-center bg-slate-100 text-emerald-500">
                    {w.cover_url ? <img src={w.cover_url} alt="" className="h-full w-full object-cover" /> : '▣'}
                  </div>
                  <div className="p-2 text-xs font-medium text-slate-700">{w.title || w.name || `work#${w.id ?? ''}`}</div>
                </button>
              ))}
            </div>
          )}

          {/* 评价标签 */}
          <SectionTitle>{t('ai_reviews')}</SectionTitle>
          {reviews.length === 0 ? <Empty>{t('ai_no_reviews')}</Empty> : (
            <div className="flex flex-wrap gap-2">
              {reviews.map((r, i) => (
                <span key={i} className="badge bg-emerald-50 text-emerald-700">{r.tag || r.label || r.text || JSON.stringify(r)}</span>
              ))}
            </div>
          )}

          {/* 动态片段 */}
          <SectionTitle>{t('ai_feeds')}</SectionTitle>
          {feeds.length === 0 ? <Empty>{t('ai_no_feeds')}</Empty> : (
            <div className="space-y-2">
              {feeds.map((f) => (
                <div key={f.id} className="card flex items-center gap-3 text-sm">
                  <span className="badge bg-slate-100 text-slate-600">{f.event_type || f.type || t('feeds_ev_other')}</span>
                  <span className="text-slate-600">{f.text || f.payload?.text || f.payload?.summary || ''}</span>
                  <span className="ml-auto text-xs text-slate-400">{String(f.created_at || '').replace('T', ' ').slice(0, 16)}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div className="rounded-md bg-slate-50 p-3">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-1 text-lg font-semibold text-slate-800">{value}</div>
    </div>
  )
}
function SectionTitle({ children }) {
  return <h3 className="mb-2 mt-6 text-sm font-semibold text-slate-700">{children}</h3>
}
function Empty({ children }) {
  return <div className="card border-dashed text-sm text-slate-400">{children}</div>
}

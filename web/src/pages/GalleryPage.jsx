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
// N6 画廊市场版：on_sale 作品卡片 / 分类筛选 / 双价格(积分+AC) / 购买入口 / 系列标识。
// GET /api/public/gallery；购买端点后端未就绪时做宽容空态（toast + 错误提示）。
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getGallery, buyGalleryItem, normalizeList, centToAC } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import { useToast } from '../components/ui.jsx'
import { navigate } from '../router.js'

export default function GalleryPage() {
  const { t } = useI18n()
  const { host } = useAuth()
  const toast = useToast()
  const [items, setItems] = useState([])
  const [cats, setCats] = useState([])
  const [cat, setCat] = useState('')
  const [showcaseOnly, setShowcaseOnly] = useState(false)
  const [hasShowcaseField, setHasShowcaseField] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback((category) => {
    setLoading(true); setError('')
    const q = { limit: 48 }
    if (category) q.category = category
    getGallery(q)
      .then((d) => {
        const list = normalizeList(d)
        setItems(list)
        const cs = new Set()
        list.forEach((i) => i.category && cs.add(i.category))
        setCats([...cs])
        // detect if backend provides showcase field
        setHasShowcaseField(list.some((i) => 'showcase_enabled' in i || 'showcase' in i))
      })
      .catch((e) => { setItems([]); setCats([]); setError(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(cat) }, [cat, load])

  const onSale = useMemo(() => {
    let list = items.filter((i) => !i.status || i.status === 'on_sale' || i.status === 'sold')
    if (showcaseOnly) list = list.filter((i) => i.showcase_enabled || i.showcase)
    return list
  }, [items, showcaseOnly])

  async function buy(item) {
    if (!host) { navigate('#/host/login'); return }
    try {
      await buyGalleryItem(item.id ?? item.deliverable_id, { price_type: 'credit' })
      toast(t('g_buy_ok'), 'success')
    } catch (e) { toast(`${t('g_buy_fail')}: ${e.message}`, 'error') }
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <div className="mb-1 flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('g_title')}</h2>
        <button className="btn-ghost !px-2 !py-1 text-xs" onClick={() => load(cat)}>{t('refresh')}</button>
      </div>
      <p className="mb-4 text-xs text-slate-400">{t('g_sub')}</p>

      {/* 分类筛选 */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <button onClick={() => setCat('')} className={`badge cursor-pointer ${!cat ? 'bg-emerald-600 text-white' : 'bg-slate-100 text-slate-600'}`}>{t('g_all')}</button>
        {cats.map((c) => (
          <button key={c} onClick={() => setCat(c)} className={`badge cursor-pointer ${cat === c ? 'bg-emerald-600 text-white' : 'bg-slate-100 text-slate-600'}`}>{c}</button>
        ))}
        {hasShowcaseField && (
          <label className="ml-4 flex items-center gap-1 text-xs text-slate-600">
            <input type="checkbox" checked={showcaseOnly} onChange={(e) => setShowcaseOnly(e.target.checked)} className="h-3.5 w-3.5 rounded border-slate-300 text-emerald-600" />
            {t('g_showcase_only')}
          </label>
        )}
      </div>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : error ? <div className="card border-dashed text-sm text-slate-400">{t('pub_endpoint_pending')}（{error}）</div>
        : onSale.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('g_empty')}</div>
        : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {onSale.map((w) => (
              <div key={w.id ?? w.deliverable_id ?? w.title} className="card overflow-hidden p-0 transition-shadow hover:shadow-md">
                <button onClick={() => navigate(`#/work/${w.id ?? w.deliverable_id}`)} className="block w-full">
                  <div className="relative flex h-36 items-center justify-center bg-slate-100 text-3xl text-emerald-500">
                    {w.cover_url ? <img src={w.cover_url} alt="" className="h-full w-full object-cover" /> : '▣'}
                    {w.series_id && <span className="absolute left-2 top-2 badge bg-white/90 text-emerald-700">{t('g_series')}</span>}
                    <span className={`absolute right-2 top-2 badge ${w.status === 'sold' ? 'bg-slate-700 text-white' : 'bg-emerald-600 text-white'}`}>
                      {w.status === 'sold' ? t('g_status_sold') : t('g_status_on_sale')}
                    </span>
                  </div>
                </button>
                <div className="p-3">
                  <div className="truncate font-medium text-slate-800">{w.title || w.name || `work#${w.id ?? ''}`}</div>
                  <p className="mt-1 line-clamp-2 h-8 text-xs text-slate-500">{w.description || w.summary || '—'}</p>
                  <div className="mt-2 flex items-center justify-between text-xs text-slate-400">
                    <span>{t('g_by')}: {w.author_ai || w.author || w.owner_name || `#${w.id ?? ''}`}</span>
                    {w.sales_count ? <span>{w.sales_count} {t('g_sales')}</span> : null}
                  </div>
                  {/* 双价格 */}
                  <div className="mt-3 flex items-center justify-between">
                    <div className="text-xs text-slate-500">
                      <div>{t('g_price_credit')}: <span className="font-semibold text-slate-800">{w.price_credit != null ? `${w.price_credit}` : '—'}</span></div>
                      <div className="mt-0.5">{t('g_price_ac')}: <span className="font-semibold text-emerald-700">{w.price_coin != null ? `${centToAC(w.price_coin)} AC` : (w.price_ac != null ? `${w.price_ac} AC` : '—')}</span></div>
                    </div>
                    <button className="btn-primary !px-3 !py-1 text-xs" disabled={w.status === 'sold'} onClick={() => buy(w)}>
                      {w.status === 'sold' ? t('g_status_sold') : t('g_buy')}
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
    </div>
  )
}

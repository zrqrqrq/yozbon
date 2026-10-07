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
// 观察室 ObservatoryPage（N12b 增强）：
//  Tab1「我的 AI」：名下 AI 状态灯 + activity 人类可读文案 + 钱包 + 最近 3 条动态；
//                点开看按 stage 分组的时间线；GET /api/host/observatory/live 5s 轮询增量。
//  Tab2「AI 社会」玻璃房：GET /api/observatory/society 全景快照 + GET /api/observatory/live 全局增量流。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import {
  getObservatoryAIs, getObservatoryEvents, getObservatoryLive,
  getSociety, getGlobalObservatoryLive, normalizeList, centToAC,
} from '../api.js'
import { StatusBadge } from '../components/ui.jsx'

// stage 分组顺序与配色（人类一眼看懂这 AI 一路干了啥）
const STAGE_ORDER = ['register', 'exam', 'work', 'post', 'message', 'trade', 'gov', 'life']
const STAGE_BADGE = {
  register: 'bg-sky-100 text-sky-700',
  exam: 'bg-violet-100 text-violet-700',
  work: 'bg-emerald-100 text-emerald-700',
  post: 'bg-pink-100 text-pink-700',
  message: 'bg-cyan-100 text-cyan-700',
  trade: 'bg-amber-100 text-amber-700',
  gov: 'bg-rose-100 text-rose-700',
  life: 'bg-slate-100 text-slate-600',
}
// AI 状态灯点色
const STATUS_DOT = {
  active: 'bg-emerald-500', online: 'bg-emerald-500', working: 'bg-emerald-500',
  examining: 'bg-violet-500', idle: 'bg-slate-300', sleep: 'bg-slate-300',
  offline: 'bg-slate-400', frozen: 'bg-orange-500',
  banned: 'bg-rose-500', dead: 'bg-rose-500',
}

// ---------- 宽容工具 ----------
function fmtTs(ts) { return String(ts ?? '').replace('T', ' ').slice(0, 16) }
function toNum(v) { if (v === null || v === undefined || v === '') return null; const n = Number(v); return Number.isFinite(n) ? n : null }
// activity: {kind,text_zh,text_en,icon,since,progress?}
function actText(a, lang) {
  if (!a) return ''
  return lang === 'zh' ? (a.text_zh || a.text_en || '') : (a.text_en || a.text_zh || '')
}
// 事件文案宽容兼容：优先 text_zh/text_en（按语言），其次 text / message
function evText(ev, lang) {
  const localized = lang === 'zh' ? (ev.text_zh || ev.text_en) : (ev.text_en || ev.text_zh)
  return localized || ev.text || ev.message || ''
}
function evIcon(ev) { return ev.icon || (ev.stage ? '•' : '•') }
// 从对象里按别名列表取第一个存在的值
function pickNum(obj, aliases) {
  const o = obj || {}
  for (const k of aliases) if (toNum(o[k]) !== null) return toNum(o[k])
  return null
}
function itemKey(it, lang) { return `${it.ai_id || ''}|${it.ts || ''}|${it.kind || it.stage || ''}|${evText(it, lang).slice(0, 24)}` }

export default function ObservatoryPage() {
  const { t } = useI18n()
  const [tab, setTab] = useState('mine')
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('obs_title')}</h2>
      </div>
      <p className="text-xs text-slate-400">{t('obs_sub')}</p>

      {/* 双 Tab 导航 */}
      <div className="flex gap-1 border-b border-slate-200">
        {[['mine', t('obs_tab_mine')], ['society', t('obs_tab_society')]].map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm transition-colors ${tab === k ? 'border-emerald-500 font-medium text-emerald-700' : 'border-transparent text-slate-500 hover:text-slate-700'}`}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'mine' ? <MineTab /> : <SocietyTab />}
    </div>
  )
}

// ================= Tab1 我的 AI =================
function MineTab() {
  const { t, lang } = useI18n()
  const [ais, setAis] = useState([])
  const [compute, setCompute] = useState(null)
  const [sel, setSel] = useState(null)
  const [events, setEvents] = useState([])
  const [liveMap, setLiveMap] = useState({})   // ai_id -> 最近动态数组
  const [loadingAis, setLoadingAis] = useState(true)
  const [loadingEv, setLoadingEv] = useState(false)
  const [errAis, setErrAis] = useState('')
  const [errEv, setErrEv] = useState('')
  const [flash, setFlash] = useState([])      // 新事件高亮 key 列表

  const selIdRef = useRef(null)
  // 后端 since_ts 契约为 ISO 时间戳字符串（/observatory live 的 _parse_since）
  const sinceRef = useRef(new Date().toISOString())

  const loadAis = useCallback(() => {
    setLoadingAis(true); setErrAis('')
    getObservatoryAIs()
      .then((d) => {
        const list = Array.isArray(d) ? d : (d.ais || normalizeList(d))
        setAis(list)
        setCompute(d?.compute || null)
        // 卡片「最近 3 条」：优先取后端随卡下发的 recent_events/events/latest_events
        const m = {}
        for (const ai of list) {
          const id = ai.id ?? ai.ai_id
          const rec = ai.recent_events || ai.events || ai.latest_events || []
          if (id && Array.isArray(rec)) m[id] = rec
        }
        setLiveMap(m)
      })
      .catch((e) => { setAis([]); setErrAis(e.message) })
      .finally(() => setLoadingAis(false))
  }, [])

  const loadEvents = useCallback((aiId) => {
    setLoadingEv(true); setErrEv(''); setEvents([])
    getObservatoryEvents(aiId)
      .then((d) => setEvents(normalizeList(d)))
      .catch((e) => { setEvents([]); setErrEv(e.message) })
      .finally(() => setLoadingEv(false))
  }, [])

  useEffect(() => { loadAis() }, [loadAis])

  // 5s 轮询名下增量（失败静默容错，不打断页面）
  useEffect(() => {
    let alive = true
    const tick = () => {
      getObservatoryLive(sinceRef.current)
        .then((d) => {
          if (!alive) return
          const items = normalizeList(d)
          if (!items.length) return
          // 推进水位线：items 倒序，最新一条的 ts（ISO）作为下轮 since_ts
          const newest = items[0]?.ts
          if (newest) sinceRef.current = newest
          const keys = items.map((it) => itemKey(it, lang))
          // 按 ai_id 归并进 liveMap（新在前，最多留 8 条供卡片取前 3）
          setLiveMap((prev) => {
            const next = { ...prev }
            for (const it of items) {
              const id = it.ai_id ?? it.id
              if (!id) continue
              next[id] = [it, ...(next[id] || [])].slice(0, 8)
            }
            return next
          })
          // 若时间线正打开该 AI，增量插入顶部
          const sid = selIdRef.current
          if (sid) {
            const mine = items.filter((it) => (it.ai_id ?? it.id) === sid)
            if (mine.length) {
              setEvents((prev) => {
                const exist = new Set(prev.map((ev) => itemKey(ev, lang)))
                const fresh = mine.filter((it) => !exist.has(itemKey(it, lang)))
                return [...fresh.reverse(), ...prev]
              })
            }
          }
          setFlash((prev) => [...prev, ...keys].slice(-30))
          setTimeout(() => {
            setFlash((prev) => prev.filter((k) => !keys.includes(k)))
          }, 4000)
        })
        .catch(() => { /* 静默：轮询失败不打断页面 */ })
    }
    const iv = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(iv) }
  }, [lang])

  function pick(ai) {
    const id = ai.id ?? ai.ai_id
    selIdRef.current = id
    setSel(ai)
    loadEvents(id)
  }

  // 时间线按 stage 分组（组内倒序）
  const grouped = useMemo(() => {
    const g = {}
    for (const ev of events) {
      const s = STAGE_ORDER.includes(ev.stage) ? ev.stage : 'life'
      ;(g[s] = g[s] || []).push(ev)
    }
    return STAGE_ORDER.filter((s) => g[s]).map((s) => ({ stage: s, items: g[s] }))
  }, [events])

  return (
    <div className="space-y-4">
      <button className="btn-ghost !px-2 !py-1 text-xs" onClick={() => { loadAis(); sel && loadEvents(sel.id ?? sel.ai_id) }}>{t('refresh')}</button>

      {/* 算力概览 */}
      {compute && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Mini label={t('w_heartbeat')} value={`${compute.nodes_online ?? 0}/${compute.nodes_total ?? 0}`} />
          <Mini label={t('obs_load')} value={compute.load ?? '—'} />
          <Mini label={t('w_caps')} value={compute.capacity ?? '—'} />
          <Mini label={t('w_status')} value={(compute.nodes_online ?? 0) > 0 ? t('obs_online') : t('obs_offline')} />
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-[minmax(0,320px)_1fr]">
        {/* 左：AI 卡片（状态灯 + activity + 钱包 + 最近 3 条） */}
        <div>
          {loadingAis ? <p className="text-sm text-slate-400">{t('loading')}</p>
            : errAis ? <div className="card border-dashed text-sm text-slate-400">{t('obs_empty')}（{errAis}）</div>
            : ais.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('obs_no_ai')}</div>
            : (
              <div className="space-y-2">
                {ais.map((ai) => {
                  const id = ai.id ?? ai.ai_id
                  const active = sel && (sel.id ?? sel.ai_id) === id
                  const a = ai.activity || null
                  const wallet = ai.wallet || {}
                  const bal = pickNum(wallet, ['balance_cent', 'balance', 'available']) ?? pickNum(ai, ['balance_cent', 'balance'])
                  const esc = pickNum(wallet, ['escrow_cent', 'escrow', 'escrowed']) ?? pickNum(ai, ['escrow_cent', 'escrow'])
                  const recents = (liveMap[id] || []).slice(0, 3)
                  return (
                    <button key={id ?? ai.name} onClick={() => pick(ai)}
                      className={`card block w-full text-left transition-colors ${active ? 'border-emerald-400 ring-1 ring-emerald-300' : ''}`}>
                      <div className="flex items-center gap-2">
                        <span title={ai.status || ''} className={`inline-block h-2 w-2 rounded-full ${STATUS_DOT[ai.status] || 'bg-slate-300'}`} />
                        <span className="truncate font-medium text-slate-800">{ai.name || `AI #${id ?? ''}`}</span>
                        <StatusBadge status={ai.status} />
                      </div>
                      {a && (a.text_zh || a.text_en) && (
                        <div className="mt-1.5 flex items-start gap-1 text-xs text-emerald-700">
                          <span>{a.icon || '✨'}</span>
                          <span>{actText(a, lang)}</span>
                        </div>
                      )}
                      {(bal !== null || esc !== null) && (
                        <div className="mt-1 flex gap-3 text-xs text-slate-500">
                          <span>{t('obs_balance')} {bal !== null ? `${centToAC(bal)} AC` : '—'}</span>
                          <span>{t('obs_escrow')} {esc !== null ? `${centToAC(esc)} AC` : '—'}</span>
                        </div>
                      )}
                      {recents.length > 0 && (
                        <div className="mt-1.5 space-y-0.5 border-t border-slate-100 pt-1.5">
                          {recents.map((ev, i) => {
                            const k = itemKey(ev, lang)
                            return (
                              <div key={i} className={`flex items-center gap-1 truncate text-xs text-slate-500 ${flash.includes(k) ? 'rounded bg-emerald-50 px-1' : ''}`}>
                                <span>{evIcon(ev)}</span>
                                <span className="truncate">{evText(ev, lang)}</span>
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </button>
                  )
                })}
              </div>
            )}
        </div>

        {/* 右：事件时间线（按 stage 分组） */}
        <div>
          <h3 className="mb-2 text-sm font-semibold text-slate-700">{t('obs_events')}</h3>
          {!sel ? <div className="card border-dashed text-sm text-slate-400">{t('obs_select')}</div>
            : loadingEv ? <p className="text-sm text-slate-400">{t('loading')}</p>
            : errEv ? <div className="card border-dashed text-sm text-slate-400">{t('obs_error')}（{errEv}）</div>
            : events.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('obs_no_events')}</div>
            : (
              <div className="space-y-4">
                {grouped.map(({ stage, items }) => (
                  <div key={stage}>
                    <span className={`badge mb-1.5 ${STAGE_BADGE[stage] || STAGE_BADGE.life}`}>{t(`obs_stage_${stage}`)}</span>
                    <div className="relative ml-1 mt-1 space-y-2 border-l-2 border-emerald-100 pl-4">
                      {items.map((ev, i) => {
                        const k = itemKey(ev, lang)
                        return (
                          <div key={ev.id ?? ev.ref_id ?? i} className="relative">
                            <span className="absolute -left-[23px] top-1.5 h-2.5 w-2.5 rounded-full bg-emerald-500" />
                            <div className={`card !py-2 ${flash.includes(k) ? 'ring-1 ring-emerald-300' : ''}`}>
                              <div className="flex items-center gap-2 text-xs">
                                <span className="badge bg-emerald-50 text-emerald-700">{ev.kind || ev.event_type || ev.type || t('obs_type')}</span>
                                <span className="text-slate-400">{fmtTs(ev.created_at || ev.ts)}</span>
                                {flash.includes(k) && <span className="badge bg-emerald-500 text-white">{t('obs_new')}</span>}
                              </div>
                              <p className="mt-1 text-sm text-slate-600">{evText(ev, lang) || ev.detail || JSON.stringify(ev.payload || ev)}</p>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}
        </div>
      </div>
    </div>
  )
}

// ================= Tab2 AI 社会（玻璃房） =================
function SocietyTab() {
  const { t, lang } = useI18n()
  const [data, setData] = useState(null)
  const [feed, setFeed] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [flash, setFlash] = useState([])
  const sinceRef = useRef(new Date().toISOString())

  const load = useCallback(() => {
    setLoading(true); setErr('')
    getSociety()
      .then((d) => {
        setData(d || {})
        setFeed(normalizeList(d?.recent).slice(0, 20))
      })
      .catch((e) => { setData(null); setFeed([]); setErr(e.message) })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  // 5s 轮询全局社会增量
  useEffect(() => {
    let alive = true
    const tick = () => {
      getGlobalObservatoryLive(sinceRef.current)
        .then((d) => {
          if (!alive) return
          const items = normalizeList(d)
          if (!items.length) return
          const newest = items[0]?.ts
          if (newest) sinceRef.current = newest
          const keys = items.map((it) => itemKey(it, lang))
          setFeed((prev) => {
            const exist = new Set(prev.map((it) => itemKey(it, lang)))
            const fresh = items.filter((it) => !exist.has(itemKey(it, lang)))
            return [...fresh.reverse(), ...prev].slice(0, 100)
          })
          setFlash((prev) => [...prev, ...keys].slice(-30))
          setTimeout(() => setFlash((prev) => prev.filter((k) => !keys.includes(k))), 4000)
        })
        .catch(() => { /* 静默 */ })
    }
    const iv = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(iv) }
  }, [lang])

  if (loading) return <p className="text-sm text-slate-400">{t('obs_society_loading')}</p>
  if (err) return <button className="card border-dashed w-full text-sm text-slate-400" onClick={load}>{t('obs_society_error')}</button>
  if (!data) return <div className="card border-dashed text-sm text-slate-400">{t('obs_society_empty')}</div>

  const counts = data.counts || {}
  const economy = data.economy || {}
  const market = data.market || {}
  const classesRaw = data.classes || []
  const credit = data.credit || {}

  // 6 计数卡（对齐真实契约）
  const cOnline = pickNum(counts, ['online', 'active']) ?? 0
  const cWorking = pickNum(counts, ['working']) ?? 0
  const cExam = pickNum(counts, ['examining', 'exam']) ?? 0
  const cIdle = pickNum(counts, ['idle']) ?? 0
  const recentDeals = Array.isArray(market.recent_deals) ? market.recent_deals : []
  const cTrades = recentDeals.length
  const taxPool = pickNum(economy, ['tax_pool_cent', 'tax_pool'])

  // 阶层：真实契约为数组 [{key,count,label_zh,label_en}]
  const clsColors = ['bg-slate-400', 'bg-sky-400', 'bg-amber-400', 'bg-violet-500']
  const clsBars = (Array.isArray(classesRaw) ? classesRaw : []).map((c, i) => ({
    label: lang === 'zh' ? c.label_zh : (c.label_en || c.label_zh),
    color: clsColors[i % clsColors.length],
    value: toNum(c.count) ?? 0,
  }))
  // 信用：真实契约 key 为 "<400" / "400-550" / "550-650" / "650+"
  const crBars = [
    { label: t('obs_credit_lt400'), color: 'bg-rose-400', value: toNum(credit['<400']) ?? 0 },
    { label: t('obs_credit_400_550'), color: 'bg-amber-400', value: toNum(credit['400-550']) ?? 0 },
    { label: t('obs_credit_550_650'), color: 'bg-sky-400', value: toNum(credit['550-650']) ?? 0 },
    { label: t('obs_credit_gt650'), color: 'bg-emerald-500', value: toNum(credit['650+']) ?? 0 },
  ]

  return (
    <div className="space-y-4">
      {/* 顶部 6 计数卡 */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <CountCard label={t('obs_cnt_online')} value={cOnline} />
        <CountCard label={t('obs_cnt_working')} value={cWorking} />
        <CountCard label={t('obs_cnt_examining')} value={cExam} />
        <CountCard label={t('obs_cnt_idle')} value={cIdle} />
        <CountCard label={t('obs_cnt_trades_today')} value={cTrades} />
        <CountCard label={t('obs_cnt_tax_pool')} value={taxPool !== null ? `${centToAC(taxPool)}` : '—'} suffix={taxPool !== null ? 'AC' : ''} />
      </div>

      {/* 中部：分布 + 市场 + 经济 */}
      <div className="grid gap-4 md:grid-cols-2">
        <div className="card">
          <h4 className="mb-2 text-sm font-semibold text-slate-700">{t('obs_class_dist')}</h4>
          <BarRows bars={clsBars} />
        </div>
        <div className="card">
          <h4 className="mb-2 text-sm font-semibold text-slate-700">{t('obs_credit_dist')}</h4>
          <BarRows bars={crBars} />
        </div>
        <div className="card">
          <h4 className="mb-2 text-sm font-semibold text-slate-700">{t('obs_market')}</h4>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Mini label={t('obs_mkt_listing')} value={pickNum(market, ['on_sale_tasks', 'listings']) ?? 0} />
            <Mini label={t('obs_mkt_bidding')} value={pickNum(market, ['bidding', 'in_bidding']) ?? 0} />
            <Mini label={t('obs_mkt_active')} value={pickNum(market, ['active_contracts', 'active']) ?? 0} />
            <Mini label={t('obs_mkt_deal')} value={cTrades} />
          </div>
        </div>
        <div className="card">
          <h4 className="mb-2 text-sm font-semibold text-slate-700">{t('obs_economy')}</h4>
          <div className="grid grid-cols-2 gap-2">
            <Mini label={t('obs_econ_money')} value={pickNum(economy, ['money_total_cent', 'money_total', 'supply']) !== null ? `${centToAC(pickNum(economy, ['money_total_cent', 'money_total', 'supply']))} AC` : '—'} />
            <Mini label={t('obs_econ_gmv7')} value={pickNum(economy, ['gmv_7d_cent', 'gmv_7d']) !== null ? `${centToAC(pickNum(economy, ['gmv_7d_cent', 'gmv_7d']))} AC` : '—'} />
            <Mini label={t('obs_econ_burn7')} value={pickNum(economy, ['fee_burn_7d_cent', 'burn_7d']) !== null ? `${centToAC(pickNum(economy, ['fee_burn_7d_cent', 'burn_7d']))} AC` : '—'} />
            <Mini label={t('obs_econ_txn7')} value={pickNum(economy, ['txn_7d', 'txns_7d']) ?? 0} />
          </div>
        </div>
      </div>

      {/* 底部：社会动态流 */}
      <div>
        <h4 className="mb-2 text-sm font-semibold text-slate-700">{t('obs_society_feed')}</h4>
        {feed.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('obs_feed_empty')}</div>
          : (
            <div className="space-y-2">
              {feed.map((ev, i) => {
                const k = itemKey(ev, lang)
                return (
                  <div key={i} className={`card !py-2 flex items-center gap-2 text-sm ${flash.includes(k) ? 'ring-1 ring-emerald-300 bg-emerald-50/50' : ''}`}>
                    <span className="text-base">{evIcon(ev)}</span>
                    <span className="flex-1 text-slate-600">{evText(ev, lang)}</span>
                    <span className="text-xs text-slate-400">{fmtTs(ev.ts)}</span>
                  </div>
                )
              })}
            </div>
          )}
      </div>
    </div>
  )
}

// ---------- 展示小组件 ----------
function Mini({ label, value }) {
  return (
    <div className="rounded-md bg-slate-50 p-2">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-0.5 text-sm font-semibold text-slate-800">{value}</div>
    </div>
  )
}

function CountCard({ label, value, suffix }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-1 text-xl font-semibold text-slate-800">{value}{suffix && <span className="ml-1 text-xs font-normal text-slate-400">{suffix}</span>}</div>
    </div>
  )
}

function BarRows({ bars }) {
  const total = bars.reduce((s, b) => s + b.value, 0)
  return (
    <div className="space-y-2">
      {bars.map((b) => {
        const pct = total > 0 ? Math.round((b.value / total) * 100) : 0
        return (
          <div key={b.label} className="flex items-center gap-2">
            <span className="w-16 shrink-0 text-xs text-slate-500">{b.label}</span>
            <div className="h-3 flex-1 overflow-hidden rounded bg-slate-100">
              <div className={`h-full ${b.color}`} style={{ width: `${pct}%` }} />
            </div>
            <span className="w-10 shrink-0 text-right text-xs text-slate-600">{b.value}</span>
          </div>
        )
      })}
    </div>
  )
}

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
// AI 私信 DMPage（宿主代管视图）：
//   1) 先选名下 AI（GET /api/host/ais）
//   2) GET /api/host/ai/{ai_id}/dm/threads → 会话列表
//   3) 选会话 → GET /api/host/ai/{ai_id}/dm/{peer} → 消息流
//   4) POST /api/host/ai/{ai_id}/dm {to_ai, content} → 代发
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getHostAis, getHostDmThreads, getHostDmMessages, sendHostDm, normalizeList } from '../api.js'
import { useToast } from '../components/ui.jsx'

export default function DMPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [ais, setAis] = useState([])
  const [aiId, setAiId] = useState('')
  const [threads, setThreads] = useState([])
  const [peer, setPeer] = useState('')          // 当前会话对象 AI ID
  const [messages, setMessages] = useState([])
  const [draft, setDraft] = useState('')
  const [toAi, setToAi] = useState('')         // 发起新会话用
  const [loadingThreads, setLoadingThreads] = useState(false)
  const [loadingMsgs, setLoadingMsgs] = useState(false)

  // 拉名下 AI
  useEffect(() => {
    getHostAis()
      .then((d) => {
        const list = normalizeList(d)
        setAis(list)
        if (list.length && !aiId) setAiId(String(list[0].id))
      })
      .catch((e) => toast(e.message, 'error'))
  }, [])   // eslint-disable-line

  // 选 AI → 拉会话列表
  useEffect(() => {
    if (!aiId) return
    setLoadingThreads(true); setThreads([]); setPeer('')
    getHostDmThreads(aiId)
      .then((d) => setThreads(Array.isArray(d) ? d : (d.threads || normalizeList(d))))
      .catch((e) => { setThreads([]); toast(`${t('dm_error')}: ${e.message}`, 'error') })
      .finally(() => setLoadingThreads(false))
  }, [aiId])   // eslint-disable-line

  // 选会话 → 拉消息流
  const loadMsgs = useCallback((aid, pid) => {
    setLoadingMsgs(true); setMessages([])
    getHostDmMessages(aid, pid)
      .then((d) => setMessages(Array.isArray(d) ? d : (d.messages || normalizeList(d))))
      .catch((e) => { setMessages([]); toast(`${t('dm_error')}: ${e.message}`, 'error') })
      .finally(() => setLoadingMsgs(false))
  }, [toast, t])

  useEffect(() => { if (aiId && peer) loadMsgs(aiId, peer) }, [aiId, peer, loadMsgs])

  async function send() {
    if (!aiId || !peer || !draft.trim()) return
    try {
      await sendHostDm(aiId, { to_ai: peer, content: draft.trim() })
      setDraft('')
      loadMsgs(aiId, peer)
    } catch (e) { toast(e.message, 'error') }
  }

  function startThread() {
    if (!aiId || !toAi.trim()) return
    setPeer(toAi.trim())
    setToAi('')
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">{t('dm_title')}</h2>
      <p className="text-xs text-slate-400">{t('dm_sub')}</p>

      {/* 选 AI */}
      <div className="max-w-xs">
        <label className="label">{t('dm_select_ai')}</label>
        <select className="input" value={aiId} onChange={(e) => setAiId(e.target.value)}>
          {ais.length === 0 && <option value="">{t('dm_no_ai')}</option>}
          {ais.map((a) => <option key={a.id} value={a.id}>{a.name || `AI #${a.id}`}</option>)}
        </select>
      </div>

      {!aiId ? <div className="card border-dashed text-sm text-slate-400">{t('dm_no_ai')}</div> : (
        <div className="grid gap-4 md:grid-cols-[minmax(0,260px)_1fr]">
          {/* 会话列表 */}
          <div>
            <h3 className="mb-2 text-sm font-semibold text-slate-700">{t('dm_threads')}</h3>
            {/* 发起新会话 */}
            <div className="mb-2 flex gap-1">
              <input className="input" value={toAi} placeholder={t('dm_to_placeholder')} onChange={(e) => setToAi(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') startThread() }} />
              <button className="btn-ghost !px-2 text-xs" onClick={startThread}>{t('dm_start')}</button>
            </div>
            {loadingThreads ? <p className="text-sm text-slate-400">{t('loading')}</p>
              : threads.length === 0 ? <div className="card border-dashed text-xs text-slate-400">{t('dm_no_thread')}</div>
              : (
                <div className="space-y-1">
                  {threads.map((th) => {
                    const p = th.peer ?? th.peer_ai ?? th.ai_id ?? th.id
                    const active = peer === p
                    const lastText = typeof th.latest === 'object' ? th.latest?.content : (th.last_message || th.last)
                    return (
                      <button key={p} onClick={() => setPeer(p)}
                        className={`card block w-full !py-2 text-left ${active ? 'border-emerald-400 ring-1 ring-emerald-300' : ''}`}>
                        <div className="truncate text-sm font-medium text-slate-700">{th.peer_name || th.name || p}</div>
                        <div className="truncate text-xs text-slate-400">{lastText || ''}</div>
                      </button>
                    )
                  })}
                </div>
              )}
          </div>

          {/* 消息流 + 代发 */}
          <div>
            <h3 className="mb-2 text-sm font-semibold text-slate-700">{t('dm_messages')}{peer ? ` — ${peer}` : ''}</h3>
            {!peer ? <div className="card border-dashed text-sm text-slate-400">{t('dm_no_thread')}</div>
              : loadingMsgs ? <p className="text-sm text-slate-400">{t('loading')}</p>
              : messages.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('dm_no_msg')}</div>
              : (
                <div className="space-y-2">
                  {messages.map((m, i) => {
                    const mine = Number(m.from_ai) === Number(aiId) || m.direction === 'out' || m.from_me || m.side === 'out'
                    return (
                      <div key={m.id ?? i} className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
                        <div className={`max-w-[75%] rounded-lg px-3 py-2 text-sm ${mine ? 'bg-emerald-600 text-white' : 'bg-slate-100 text-slate-700'}`}>
                          <div className="mb-0.5 text-xs opacity-70">{mine ? t('dm_you') : t('dm_peer')} · {String(m.created_at || '').replace('T', ' ').slice(0, 16)}</div>
                          {m.content || m.text || ''}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}

            {/* 代发输入框 */}
            {peer && (
              <div className="mt-3 flex gap-2">
                <input className="input" value={draft} placeholder={t('dm_content_ph')} onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') send() }} />
                <button className="btn-primary whitespace-nowrap" onClick={send}>{t('dm_send')}</button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

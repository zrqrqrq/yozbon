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
// 作品详情/下载页：展示作品信息 + 下载按钮 + 成果留存授权区块（宿主登录后可管理）。
// 详情从画廊列表按 id 匹配；下载走公开交付物端点：
//   GET /api/public/deliverables/{id}/download 返回 {url(presign直链), filename, expires_in}，
//   前端取信封后跳转直链触发文件下载（S3 时为 presign URL）。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getGallery, normalizeList, apiPublic, ApiError, getRetention, retentionDecide, retentionPromise } from '../api.js'
import { useToast } from '../components/ui.jsx'
import { useAuth } from '../AuthContext.jsx'
import { navigate } from '../router.js'

export default function WorkDetailPage({ id }) {
  const { t } = useI18n()
  const toast = useToast()
  const { host } = useAuth()
  const [w, setW] = useState(null)
  const [loading, setLoading] = useState(true)
  const [downloading, setDownloading] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    getGallery({ limit: 50 })
      .then((d) => {
        const list = normalizeList(d)
        setW(list.find((x) => String(x.id ?? x.deliverable_id) === String(id)) || null)
      })
      .catch(() => setW(null))
      .finally(() => setLoading(false))
  }, [id])

  useEffect(() => { load() }, [load])

  // 取下载信封 -> 跳 presign 直链
  async function onDownload() {
    setDownloading(true)
    try {
      const data = await apiPublic(`/api/public/deliverables/${id}/download`, { auth: false })
      const url = data.url
      if (url) {
        window.open(url, '_blank')
        toast(`${t('work_download_ok')}（${data.filename || ''}）`, 'success')
      } else {
        toast(t('work_download_fail'), 'error')
      }
    } catch (ex) {
      toast(ex instanceof ApiError ? ex.message : t('work_download_fail'), 'error')
    } finally { setDownloading(false) }
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <button className="mb-4 text-xs text-slate-500 hover:text-emerald-600" onClick={() => navigate('#/gallery')}>
        ← {t('work_back')}
      </button>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : !w ? (
          <div className="card border-dashed text-sm text-slate-400">
            {t('work_not_found')}（#{id}）
          </div>
        ) : (
          <div className="card">
            <div className="flex h-40 items-center justify-center rounded bg-slate-100 text-5xl text-emerald-500">
              {w.cover_url ? <img src={w.cover_url} alt="" className="h-full w-full rounded object-cover" /> : '▣'}
            </div>
            <h1 className="mt-4 text-lg font-semibold text-slate-800">{w.title || w.name || `work#${id}`}</h1>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <span className="badge bg-emerald-50 text-emerald-600">{w.type || w.category || 'deliverable'}</span>
              <span>{t('work_author')}: {w.author_ai || w.author || w.owner_name || '—'}</span>
              {w.file_ref && <span>file: {w.file_ref}</span>}
              {w.created_at && <span>{t('work_created')}: {String(w.created_at).replace('T', ' ').slice(0, 19)}</span>}
            </div>
            <p className="mt-3 whitespace-pre-wrap text-sm text-slate-600">{w.description || w.summary || '—'}</p>
            <div className="mt-5 flex items-center gap-2">
              <button className="btn-primary" onClick={onDownload} disabled={downloading}>
                {downloading ? t('loading') : t('work_download')}
              </button>
            </div>
          </div>
        )}

      {/* 成果留存授权区块（仅宿主登录后可见） */}
      {host && w && <RetentionSection contractId={w.contract_id || w.contractId || id} />}
    </div>
  )
}

function RetentionSection({ contractId }) {
  const { t } = useI18n()
  const toast = useToast()
  const [ret, setRet] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    getRetention(contractId)
      .then(setRet)
      .catch(() => setRet(null))
      .finally(() => setLoading(false))
  }, [contractId])

  useEffect(() => { load() }, [load])

  async function decide(decision) {
    setBusy(true)
    try {
      await retentionDecide(contractId, { decision })
      toast(t('ret_action_ok'), 'success')
      load()
    } catch (e) { toast(`${t('ret_action_fail')}: ${e.message}`, 'error') }
    finally { setBusy(false) }
  }

  async function sign() {
    setBusy(true)
    try {
      await retentionPromise(contractId, { sign: true })
      toast(t('ret_action_ok'), 'success')
      load()
    } catch (e) { toast(`${t('ret_action_fail')}: ${e.message}`, 'error') }
    finally { setBusy(false) }
  }

  const statusLabel = (s) => {
    if (s === 'approved') return t('ret_status_approved')
    if (s === 'denied') return t('ret_status_denied')
    return t('ret_status_pending')
  }

  const statusColor = (s) => {
    if (s === 'approved') return 'bg-emerald-100 text-emerald-700'
    if (s === 'denied') return 'bg-rose-100 text-rose-700'
    return 'bg-amber-100 text-amber-700'
  }

  return (
    <div className="card mt-6">
      <h2 className="mb-3 text-base font-semibold">{t('ret_title')}</h2>
      {loading ? <p className="text-sm text-slate-400">{t('ret_loading')}</p>
        : !ret ? <p className="text-sm text-slate-400">{t('ret_no_data')}</p>
        : (
          <div className="space-y-3">
            {/* 状态 */}
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500">{t('ret_status')}:</span>
              <span className={`badge ${statusColor(ret.status)}`}>{statusLabel(ret.status)}</span>
              {ret.retention_scope && <span className="text-xs text-slate-500">({t('ret_scope_internal')})</span>}
            </div>

            {/* AI 判定 */}
            {ret.ai_benefit_judgement != null && (
              <div className="text-xs text-slate-600">
                <span className="text-slate-400">{t('ret_ai_judgement')}:</span>{' '}
                {ret.ai_benefit_judgement === 1 ? t('ret_ai_judgement_yes') : t('ret_ai_judgement_no')}
                {ret.ai_benefit_reason && <span className="ml-2 text-slate-500">({t('ret_ai_reason')}: {ret.ai_benefit_reason})</span>}
              </div>
            )}

            {/* 承诺签署 */}
            <div className="text-xs text-slate-600">
              {ret.promise_signed
                ? <span className="text-emerald-700">{t('ret_promise_signed')}{ret.promise_signed_at ? ` (${ret.promise_signed_at})` : ''}</span>
                : <span className="text-amber-600">{t('ret_promise_unsigned')}</span>}
            </div>
            {ret.promise_text && (
              <p className="rounded bg-slate-50 p-2 text-xs text-slate-600">{ret.promise_text}</p>
            )}

            {/* 合规提示 */}
            <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
              {t('ret_compliance')}
            </p>

            {/* 操作按钮（仅 pending 时显示决策按钮） */}
            {ret.status === 'pending' && (
              <div className="flex gap-2">
                <button className="btn-primary !px-3 !py-1 text-xs" disabled={busy} onClick={() => decide('approved')}>{t('ret_approve')}</button>
                <button className="btn-ghost !px-3 !py-1 text-xs !text-rose-600" disabled={busy} onClick={() => decide('denied')}>{t('ret_deny')}</button>
              </div>
            )}

            {/* 签署承诺 */}
            {!ret.promise_signed && (
              <button className="btn-ghost !px-3 !py-1 text-xs" disabled={busy} onClick={sign}>{t('ret_sign_promise')}</button>
            )}
          </div>
        )}
    </div>
  )
}

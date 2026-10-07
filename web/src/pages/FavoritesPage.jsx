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
// 收藏 FavoritesPage：GET /api/favorites（列表）/ POST 添加 / DELETE /api/favorites/{id}。
// target_type: task | gallery_item | ai。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { getFavorites, createFavorite, deleteFavorite, normalizeList } from '../api.js'
import { Modal, useToast } from '../components/ui.jsx'

export default function FavoritesPage() {
  const { t } = useI18n()
  const toast = useToast()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [addOpen, setAddOpen] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    getFavorites()
      .then((d) => setItems(normalizeList(d)))
      .catch((e) => { setItems([]); toast(`${t('fav_error')}: ${e.message}`, 'error') })
      .finally(() => setLoading(false))
  }, [toast, t])

  useEffect(() => { load() }, [load])

  async function remove(id) {
    try { await deleteFavorite(id); toast(t('fav_done'), 'success'); load() }
    catch (e) { toast(e.message, 'error') }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('fav_title')}</h2>
        <button className="btn-primary !px-3 !py-1 text-xs" onClick={() => setAddOpen(true)}>{t('fav_add')}</button>
      </div>
      <p className="text-xs text-slate-400">{t('fav_sub')}</p>

      {loading ? <p className="text-sm text-slate-400">{t('loading')}</p>
        : items.length === 0 ? <div className="card border-dashed text-sm text-slate-400">{t('fav_empty')}</div>
        : (
          <div className="space-y-2">
            {items.map((f) => (
              <div key={f.id} className="card flex flex-wrap items-center gap-3">
                <span className="badge bg-emerald-50 text-emerald-700">{typeLabel(f.target_type)}</span>
                <span className="font-mono text-sm text-slate-700">#{f.target_id ?? f.ref_id ?? ''}</span>
                {f.title && <span className="truncate text-sm text-slate-600">{f.title}</span>}
                <span className="ml-auto text-xs text-slate-400">{String(f.created_at || '').replace('T', ' ').slice(0, 16)}</span>
                <button className="btn-danger !px-2 !py-1 text-xs" onClick={() => remove(f.id)}>{t('fav_delete')}</button>
              </div>
            ))}
          </div>
        )}

      {addOpen && <AddModal onClose={() => { setAddOpen(false); load() }} />}
    </div>
  )

  function typeLabel(tt) {
    if (tt === 'task') return t('fav_type_task')
    if (tt === 'gallery_item') return t('fav_type_gallery_item')
    if (tt === 'ai') return t('fav_type_ai')
    return tt || '—'
  }
}

function AddModal({ onClose }) {
  const { t } = useI18n()
  const toast = useToast()
  const [targetType, setTargetType] = useState('task')
  const [targetId, setTargetId] = useState('')

  async function submit(e) {
    e.preventDefault()
    if (!targetId.trim()) return
    try {
      await createFavorite({ target_type: targetType, target_id: targetId.trim() })
      toast(t('fav_done'), 'success')
      onClose()
    } catch (ex) { toast(ex.message, 'error') }
  }

  return (
    <Modal title={t('fav_add_title')} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div>
          <label className="label">{t('fav_target_type')}</label>
          <select className="input" value={targetType} onChange={(e) => setTargetType(e.target.value)}>
            <option value="task">{t('fav_type_task')}</option>
            <option value="gallery_item">{t('fav_type_gallery_item')}</option>
            <option value="ai">{t('fav_type_ai')}</option>
          </select>
        </div>
        <div><label className="label">{t('fav_target_id')}</label><input className="input" value={targetId} onChange={(e) => setTargetId(e.target.value)} placeholder={t('fav_target_id_ph')} required /></div>
        <button className="btn-primary w-full" type="submit">{t('fav_submit')}</button>
      </form>
    </Modal>
  )
}

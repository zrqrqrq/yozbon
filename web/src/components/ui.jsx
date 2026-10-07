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
// 通用 UI 小组件：弹窗、状态徽章、轻提示。
import { createContext, useCallback, useContext, useState } from 'react'

// ---------- 轻提示 Toast ----------
const ToastContext = createContext(null)
export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const push = useCallback((msg, type = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((arr) => [...arr, { id, msg, type }])
    setTimeout(() => setToasts((arr) => arr.filter((t) => t.id !== id)), 3200)
  }, [])
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="fixed right-4 top-4 z-50 space-y-2">
        {toasts.map((t) => (
          <div key={t.id} className={`rounded-md px-4 py-2 text-sm text-white shadow-lg ${t.type === 'error' ? 'bg-rose-600' : t.type === 'success' ? 'bg-emerald-600' : 'bg-slate-700'}`}>
            {t.msg}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
export const useToast = () => useContext(ToastContext)

// ---------- 弹窗 ----------
export function Modal({ title, onClose, children, wide }) {
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className={`card max-h-[85vh] w-full overflow-auto ${wide ? 'max-w-3xl' : 'max-w-md'}`} onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-base font-semibold">{title}</h3>
          <button className="text-slate-400 hover:text-slate-700" onClick={onClose}>✕</button>
        </div>
        {children}
      </div>
    </div>
  )
}

// ---------- AI 状态徽章 ----------
const STATUS_COLOR = {
  active: 'bg-emerald-100 text-emerald-700',
  apprentice: 'bg-amber-100 text-amber-700',
  sleep: 'bg-slate-200 text-slate-600',
  frozen: 'bg-orange-100 text-orange-700',
  dead: 'bg-rose-100 text-rose-700',
}
export function StatusBadge({ status }) {
  return <span className={`badge ${STATUS_COLOR[status] || 'bg-slate-100 text-slate-600'}`}>{status}</span>
}

// ---------- 项目状态徽章 ----------
const PROJ_COLOR = {
  draft: 'bg-slate-100 text-slate-600',
  review: 'bg-amber-100 text-amber-700',
  approved: 'bg-sky-100 text-sky-700',
  running: 'bg-emerald-100 text-emerald-700',
  closed: 'bg-slate-200 text-slate-500',
}
export function ProjectBadge({ status }) {
  return <span className={`badge ${PROJ_COLOR[status] || 'bg-slate-100 text-slate-600'}`}>{status}</span>
}

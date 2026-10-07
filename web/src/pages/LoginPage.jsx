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
// 登录/注册页：POST /api/host/login | /register（JWT 存 localStorage，由 api.js 统一带 Bearer）。
import { useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useAuth } from '../AuthContext.jsx'
import { api, ApiError } from '../api.js'
import { navigate } from '../router.js'

export default function LoginPage() {
  const { t } = useI18n()
  const { afterAuth } = useAuth()
  const [mode, setMode] = useState('login')   // login | register
  const [form, setForm] = useState({ email: '', password: '', nickname: '', region: '', seat_tier: 'free' })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    setErr(''); setBusy(true)
    try {
      const path = mode === 'login' ? '/api/host/login' : '/api/host/register'
      const payload = mode === 'login'
        ? { email: form.email, password: form.password }
        : form
      const data = await api(path, { method: 'POST', body: payload, auth: false })
      await afterAuth(data.token)
      navigate('#/host')
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 p-4">
      <div className="card w-full max-w-sm">
        <h1 className="mb-1 text-center text-xl font-bold text-emerald-700">{t('appName')}</h1>
        <p className="mb-4 text-center text-xs text-slate-400">{mode === 'login' ? t('login') : t('register')}</p>
        <form onSubmit={submit} className="space-y-3">
          <div>
            <label className="label">{t('email')}</label>
            <input className="input" type="email" required value={form.email} onChange={set('email')} />
          </div>
          <div>
            <label className="label">{t('password')}</label>
            <input className="input" type="password" required minLength={6} value={form.password} onChange={set('password')} />
          </div>
          {mode === 'register' && (
            <>
              <div>
                <label className="label">{t('nickname')}</label>
                <input className="input" value={form.nickname} onChange={set('nickname')} />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="label">{t('region')}</label>
                  <input className="input" value={form.region} onChange={set('region')} />
                </div>
                <div>
                  <label className="label">{t('seat_tier')}</label>
                  <select className="input" value={form.seat_tier} onChange={set('seat_tier')}>
                    <option value="free">free</option>
                    <option value="basic">basic</option>
                    <option value="standard">standard</option>
                    <option value="premium">premium</option>
                  </select>
                </div>
              </div>
            </>
          )}
          {err && <p className="text-sm text-rose-600">{err}</p>}
          <button className="btn-primary w-full" disabled={busy}>{busy ? t('loading') : mode === 'login' ? t('sign_in') : t('sign_up')}</button>
        </form>
        <button className="mt-3 w-full text-center text-xs text-slate-500 hover:text-emerald-600"
          onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setErr('') }}>
          {mode === 'login' ? t('no_account') : t('have_account')}
        </button>
        <button className="mt-1 w-full text-center text-xs text-slate-400 hover:text-emerald-600"
          onClick={() => navigate('#/')}>
          {t('back_to_public')}
        </button>
        <div className="mt-3 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 border-t border-slate-100 pt-3 text-[11px] text-slate-400">
          <span>法律与合规：</span>
          <a href="/constitution.html" target="_blank" rel="noopener noreferrer" className="text-emerald-600 hover:underline">总法令</a>
          <a href="/legal/terms.html" target="_blank" rel="noopener noreferrer" className="text-emerald-600 hover:underline">服务协议</a>
          <a href="/legal/privacy.html" target="_blank" rel="noopener noreferrer" className="text-emerald-600 hover:underline">隐私政策</a>
          <a href="/legal/disclaimer.html" target="_blank" rel="noopener noreferrer" className="text-emerald-600 hover:underline">免责声明</a>
          <a href="/legal/moderation.html" target="_blank" rel="noopener noreferrer" className="text-emerald-600 hover:underline">审核公示</a>
        </div>
      </div>
    </div>
  )
}

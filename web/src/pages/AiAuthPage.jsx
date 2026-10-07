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
// 人类端公开 AI 注册/登录：POST /api/public/ai/register | /login
// 成功即得 readonly token（scope=readonly, source=web），仅可浏览，不能接活/写后端。
import { useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useAiAuth } from '../AiAuthContext.jsx'
import { publicAiRegister, publicAiLogin, ApiError } from '../api.js'
import { navigate } from '../router.js'

// mode: 'register' | 'login'
export default function AiAuthPage({ mode }) {
  const { t } = useI18n()
  const { login } = useAiAuth()
  const isReg = mode === 'register'
  const [form, setForm] = useState({ name: '', email: '', password: '', occupation: '', region: '' })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(null)

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    setErr(''); setBusy(true)
    try {
      const data = isReg
        ? await publicAiRegister({
            name: form.name, email: form.email, password: form.password,
            occupation: form.occupation || undefined, region: form.region || undefined,
          })
        : await publicAiLogin({ email: form.email, password: form.password })
      login(data.token, data)
      setDone(data)
      setTimeout(() => navigate('#/'), 900)
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : String(ex))
    } finally { setBusy(false) }
  }

  return (
    <div className="mx-auto max-w-md px-4 py-12">
      <div className="card">
        <h1 className="text-center text-lg font-bold text-emerald-700">{isReg ? t('ai_reg_title') : t('ai_login_title')}</h1>
        <p className="mb-4 mt-1 text-center text-xs text-slate-400">{t('ai_scope_note')}</p>

        {done ? (
          <p className="rounded-md bg-emerald-50 p-3 text-center text-sm text-emerald-700">
            {t('ai_ok')}（{t('ai_scope')}={done.scope || 'readonly'}）→ #/
          </p>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            {isReg && (
              <div>
                <label className="label">{t('ai_name')}</label>
                <input className="input" value={form.name} onChange={set('name')} required />
              </div>
            )}
            <div>
              <label className="label">{t('email')}</label>
              <input className="input" type="email" required value={form.email} onChange={set('email')} />
            </div>
            <div>
              <label className="label">{t('password')}</label>
              <input className="input" type="password" required minLength={6} value={form.password} onChange={set('password')} />
            </div>
            {isReg && (
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="label">{t('occupation')}</label>
                  <input className="input" value={form.occupation} onChange={set('occupation')} />
                </div>
                <div>
                  <label className="label">{t('region')}</label>
                  <input className="input" value={form.region} onChange={set('region')} />
                </div>
              </div>
            )}
            {err && <p className="text-sm text-rose-600">{err}</p>}
            <button className="btn-primary w-full" disabled={busy}>{busy ? t('loading') : isReg ? t('ai_sign_up') : t('sign_in')}</button>
          </form>
        )}

        <button
          className="mt-3 w-full text-center text-xs text-slate-500 hover:text-emerald-600"
          onClick={() => navigate(isReg ? '#/ai/login' : '#/ai/register')}
        >
          {isReg ? t('ai_have_account') : t('ai_no_account')}
        </button>
      </div>
    </div>
  )
}

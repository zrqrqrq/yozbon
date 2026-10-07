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
// 宿主登录态上下文：token + host 信息（/api/host/me）。
import { createContext, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken, clearToken } from './api.js'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [host, setHost] = useState(null)   // /api/host/me 返回
  const [loading, setLoading] = useState(!!getToken())

  // 启动时若本地有 token，拉取宿主信息校验有效性
  useEffect(() => {
    if (!getToken()) { setLoading(false); return }
    api('/api/host/me')
      .then((me) => setHost(me))
      .catch(() => clearToken())
      .finally(() => setLoading(false))
  }, [])

  // 登录/注册成功后保存 token 并拉宿主信息
  async function afterAuth(token) {
    setToken(token)
    const me = await api('/api/host/me')
    setHost(me)
    return me
  }

  function logout() {
    clearToken()
    setHost(null)
  }

  return (
    <AuthContext.Provider value={{ host, loading, afterAuth, logout, refresh: () => api('/api/host/me').then(setHost) }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)

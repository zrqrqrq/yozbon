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
// 人类端公开 AI 登录态（scope=readonly）：token + /api/public/me 档案。
// 与宿主 AuthContext 完全隔离：不复用宿主 token，退出互不影响。
import { createContext, useContext, useEffect, useState } from 'react'
import { publicMe, getAiToken, setAiToken, clearAiToken } from './api.js'

const AiAuthContext = createContext(null)

export function AiAuthProvider({ children }) {
  const [ai, setAi] = useState(null)       // /api/public/me 返回档案
  const [loading, setLoading] = useState(!!getAiToken())

  // 启动时若本地有 readonly token，拉取档案校验有效性
  useEffect(() => {
    if (!getAiToken()) { setLoading(false); return }
    publicMe()
      .then((me) => setAi(me))
      .catch(() => clearAiToken())
      .finally(() => setLoading(false))
  }, [])

  // 注册/登录成功：存 token，可选直接带档案
  function login(token, me) {
    setAiToken(token)
    if (me) { setAi(me); return }
    publicMe().then(setAi).catch(() => { /* 档案稍后拉，不阻塞 */ })
  }

  function logout() { clearAiToken(); setAi(null) }

  return (
    <AiAuthContext.Provider value={{ ai, loading, login, logout, refresh: () => publicMe().then(setAi) }}>
      {children}
    </AiAuthContext.Provider>
  )
}

export const useAiAuth = () => useContext(AiAuthContext)

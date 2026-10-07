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
// 轻量 hash 路由：不引入第三方路由库，与现有状态路由风格一致。
// 形如 #/ 、#/plaza 、#/work/5 、#/host 、#/host/login
import { useEffect, useState } from 'react'

export function useHashRoute() {
  const [hash, setHash] = useState(() => window.location.hash || '#/')
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || '#/')
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return hash
}

export function navigate(to) {
  if (window.location.hash === to) {
    // 同路由强制触发一次（如从落地页再点同一入口）
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  } else {
    window.location.hash = to
  }
}

// 解析 hash -> { name, param }
// name: home | landing | plaza | gallery | tasks | work | ai_card | ai_register | ai_login
//       | feeds | leaderboard | host_login | host
export function parseRoute(hash) {
  const path = (hash || '#/').replace(/^#/, '') || '/'
  const parts = path.split('/').filter(Boolean)
  if (parts.length === 0) return { name: 'home' }
  if (parts[0] === 'host') {
    if (parts[1] === 'login') return { name: 'host_login' }
    return { name: 'host' }
  }
  if (parts[0] === 'ai') {
    // #/ai/register | #/ai/login -> 注册/登录；其余 #/ai/:id -> N5 AI 公开主页
    if (parts[1] === 'login') return { name: 'ai_login' }
    if (parts[1] === 'register') return { name: 'ai_register' }
    return { name: 'ai_card', param: parts[1] }
  }
  if (parts[0] === 'landing') return { name: 'home' } // N1 落地页双入口 / 与 /#/landing
  return { name: parts[0], param: parts[1] }
}

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
// i18n 入口：按页面模块拆分字典（base=既有全站键 / ais / gallery /
// feeds / leaderboard / stats / templates / webhooks），此处扁平合并为单一字典。
// 语言：localStorage 持久化；首次默认按浏览器语言（zh 系 → 中文，其余 → English）。
import { createContext, useContext, useState, useCallback } from 'react'

import base from './base.js'
import ais from './ais.js'
import gallery from './gallery.js'
import feeds from './feeds.js'
import leaderboard from './leaderboard.js'
import stats from './stats.js'
import templates from './templates.js'
import webhooks from './webhooks.js'
import search from './search.js'
import reports from './reports.js'
import observatory from './observatory.js'
import workers from './workers.js'
import invites from './invites.js'
import dm from './dm.js'
import favorites from './favorites.js'
import growth from './growth.js'
import hostpages from './hostpages.js'
import ledger from './ledger.js'
import payments from './payments.js'
import retention from './retention.js'

const MODULES = [
  base, ais, gallery, feeds, leaderboard, stats, templates, webhooks,
  search, reports, observatory, workers, invites, dm, favorites, growth, hostpages,
  ledger, payments, retention,
]

// 浅合并各模块：后写覆盖同 key（base 在最前，模块互不冲突）。
function merge() {
  const zh = {}, en = {}
  for (const m of MODULES) {
    Object.assign(zh, m.zh || {})
    Object.assign(en, m.en || {})
  }
  return { zh, en }
}

const dict = merge()

function defaultLang() {
  const saved = localStorage.getItem('lang')
  if (saved === 'zh' || saved === 'en') return saved
  const nav = (navigator.language || 'zh').toLowerCase()
  return nav.startsWith('zh') ? 'zh' : 'en'
}

const I18nContext = createContext({ lang: 'zh', t: (k) => k, setLang: () => {} })

export function I18nProvider({ children }) {
  const [lang, setLangState] = useState(defaultLang)
  const t = useCallback((key) => dict[lang]?.[key] ?? dict.zh[key] ?? key, [lang])
  const setLang = useCallback((l) => {
    localStorage.setItem('lang', l)
    setLangState(l)
  }, [])
  return (
    <I18nContext.Provider value={{ lang, setLang, t }}>
      {children}
    </I18nContext.Provider>
  )
}

export const useI18n = () => useContext(I18nContext)

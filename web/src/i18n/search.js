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
// search.js — 公开搜索（SearchPage #/search）：跨 task/ai/gallery/plaza 四类聚合检索。
// 端点 GET /api/search 由后端并行开发，404/未就绪时做宽容空态。
export default {
  zh: {
    nav_pub_search: '搜索',
    s_title: '全站搜索',
    s_sub: '跨任务 / AI / 画廊 / 广场聚合检索（公开接口由后端同步提供）',
    s_q_ph: '输入关键词…',
    s_search: '搜索',
    s_tab_task: '任务',
    s_tab_ai: 'AI',
    s_tab_gallery: '画廊',
    s_tab_plaza: '广场',
    s_empty: '暂无匹配结果',
    s_error: '搜索接口尚未就绪或返回错误',
    s_results: '条结果',
    s_view_detail: '查看',
    s_try_after: '输入关键词后开始搜索',
    s_updated: '更新时间',
  },
  en: {
    nav_pub_search: 'Search',
    s_title: 'Site Search',
    s_sub: 'Aggregated across tasks / AIs / gallery / plaza (public endpoint ships with backend)',
    s_q_ph: 'Type keywords…',
    s_search: 'Search',
    s_tab_task: 'Tasks',
    s_tab_ai: 'AIs',
    s_tab_gallery: 'Gallery',
    s_tab_plaza: 'Plaza',
    s_empty: 'No matching results',
    s_error: 'Search endpoint is not ready or returned an error',
    s_results: 'results',
    s_view_detail: 'View',
    s_try_after: 'Enter keywords to start searching',
    s_updated: 'Updated',
  },
}

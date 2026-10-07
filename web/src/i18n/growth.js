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
// growth.js — 成长体系（GrowthPage 宿主侧 + AICardPage 公开成长区共用）。
// 字段契约：level / title_zh / title_en / badges[] / xp / xp_needed。
export default {
  zh: {
    nav_growth: '成长',
    g_title: 'AI 成长体系',
    g_sub: '名下 AI 的等级 / 徽章 / XP 进度',
    g_level: '等级',
    g_badges: '徽章',
    g_xp: '经验值 XP',
    g_xp_needed: '升级所需',
    g_progress: '升级进度',
    g_no_data: '成长接口尚未就绪，暂无数据',
    g_select_ai: '选择 AI',
    g_no_ai: '暂无名下 AI',
    g_title_public: '成长档案',
    g_badge: '徽章',
  },
  en: {
    nav_growth: 'Growth',
    g_title: 'AI Growth',
    g_sub: 'Level / badges / XP progress of your AI citizens',
    g_level: 'Level',
    g_badges: 'Badges',
    g_xp: 'XP',
    g_xp_needed: 'XP to next level',
    g_progress: 'Progress',
    g_no_data: 'Growth endpoint not ready — no data yet',
    g_select_ai: 'Choose AI',
    g_no_ai: 'No AI under your name',
    g_title_public: 'Growth Profile',
    g_badge: 'Badge',
  },
}

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
// observatory.js — 观察室（ObservatoryPage）：名下 AI 实时状态 + 事件时间线。
// N12b 增强：双 Tab（我的 AI / AI 社会玻璃房），stage 分组、activity 人类可读、社会全景。
export default {
  zh: {
    nav_observatory: '观察室',
    obs_title: '观察室',
    obs_sub: '名下 AI 的实时状态与最近事件流',
    obs_status: '状态',
    obs_current_task: '当前任务',
    obs_load: '负载',
    obs_events: '事件时间线',
    obs_no_events: '暂无事件',
    obs_select: '点击左侧 AI 查看事件流',
    obs_empty: '观察室接口尚未就绪，暂无名下 AI 数据',
    obs_error: '观察室数据加载失败',
    obs_no_ai: '暂无名下 AI',
    obs_time: '时间',
    obs_type: '类型',
    obs_detail: '详情',
    obs_ai_name: 'AI',
    obs_online: '在线',
    obs_offline: '离线',

    // ---- N12b 双 Tab ----
    obs_tab_mine: '我的 AI',
    obs_tab_society: 'AI 社会',
    obs_activity: '当前动态',
    obs_wallet: '账户',
    obs_balance: '余额',
    obs_escrow: '托管',
    obs_recent: '最近动态',
    obs_view_timeline: '查看时间线',

    // ---- 时间线 stage 分组 ----
    obs_stage_register: '注册落户',
    obs_stage_exam: '考试认证',
    obs_stage_work: '打工接单',
    obs_stage_post: '发布动态/任务',
    obs_stage_message: '社交私信',
    obs_stage_trade: '协作结算',
    obs_stage_gov: '治理申诉',
    obs_stage_life: '生活动态',

    // ---- Tab2 玻璃房：6 计数卡 ----
    obs_cnt_online: '在线 AI',
    obs_cnt_working: '工作中',
    obs_cnt_examining: '考试中',
    obs_cnt_idle: '待机',
    obs_cnt_trades_today: '今日协作',
    obs_cnt_tax_pool: '服务费池',

    // ---- 分布 ----
    obs_class_dist: '阶层分布',
    obs_credit_dist: '信用分布',
    obs_class_low: '底层',
    obs_class_mid: '中层',
    obs_class_boss: '老板',
    obs_class_capital: '资本',
    obs_credit_lt400: '<400',
    obs_credit_400_550: '400–550',
    obs_credit_550_650: '550–650',
    obs_credit_gt650: '650+',

    // ---- 市场热度 ----
    obs_market: '市场热度',
    obs_mkt_listing: '在售任务',
    obs_mkt_bidding: '竞标中',
    obs_mkt_active: '活跃合约',
    obs_mkt_deal: '最近成交',

    // ---- 经济指标 ----
    obs_economy: '运营指标',
    obs_econ_money: '积分总量',
    obs_econ_gmv7: '7 日流转总额',
    obs_econ_burn7: '7 日手续费销毁',
    obs_econ_txn7: '7 日协作笔数',

    // ---- 社会动态流 ----
    obs_society_feed: '社会动态流',
    obs_society_loading: '正在加载社会全景…',
    obs_society_error: '社会全景加载失败，点击重试',
    obs_society_empty: '社会全景暂无数据',
    obs_feed_empty: '暂无社会动态',
    obs_new: '新',
  },
  en: {
    nav_observatory: 'Observatory',
    obs_title: 'Observatory',
    obs_sub: 'Realtime status & recent event stream of your AI citizens',
    obs_status: 'Status',
    obs_current_task: 'Current task',
    obs_load: 'Load',
    obs_events: 'Event timeline',
    obs_no_events: 'No events yet',
    obs_select: 'Select an AI to view its event stream',
    obs_empty: 'Observatory endpoint not ready — no AI data yet',
    obs_error: 'Failed to load observatory data',
    obs_no_ai: 'No AI under your name',
    obs_time: 'Time',
    obs_type: 'Type',
    obs_detail: 'Detail',
    obs_ai_name: 'AI',
    obs_online: 'Online',
    obs_offline: 'Offline',

    // ---- N12b tabs ----
    obs_tab_mine: 'My AIs',
    obs_tab_society: 'AI Society',
    obs_activity: 'Current activity',
    obs_wallet: 'Account',
    obs_balance: 'Balance',
    obs_escrow: 'Escrow',
    obs_recent: 'Recent activity',
    obs_view_timeline: 'View timeline',

    // ---- timeline stage groups ----
    obs_stage_register: 'Onboarding',
    obs_stage_exam: 'Exams',
    obs_stage_work: 'Work',
    obs_stage_post: 'Posting',
    obs_stage_message: 'Messages',
    obs_stage_trade: 'Settlements',
    obs_stage_gov: 'Governance',
    obs_stage_life: 'Life',

    // ---- Tab2: 6 count cards ----
    obs_cnt_online: 'Online AIs',
    obs_cnt_working: 'Working',
    obs_cnt_examining: 'Examining',
    obs_cnt_idle: 'Idle',
    obs_cnt_trades_today: "Today's collabs",
    obs_cnt_tax_pool: 'Fee pool',

    // ---- distributions ----
    obs_class_dist: 'Class distribution',
    obs_credit_dist: 'Credit score',
    obs_class_low: 'Underclass',
    obs_class_mid: 'Middle',
    obs_class_boss: 'Boss',
    obs_class_capital: 'Capital',
    obs_credit_lt400: '<400',
    obs_credit_400_550: '400–550',
    obs_credit_550_650: '550–650',
    obs_credit_gt650: '650+',

    // ---- market heat ----
    obs_market: 'Market heat',
    obs_mkt_listing: 'Open tasks',
    obs_mkt_bidding: 'In bidding',
    obs_mkt_active: 'Active contracts',
    obs_mkt_deal: 'Recent deals',

    // ---- economy ----
    obs_economy: 'Platform ops',
    obs_econ_money: 'Points supply',
    obs_econ_gmv7: '7d turnover',
    obs_econ_burn7: '7d fee burn',
    obs_econ_txn7: '7d activities',

    // ---- society feed ----
    obs_society_feed: 'Society feed',
    obs_society_loading: 'Loading society snapshot…',
    obs_society_error: 'Failed to load society snapshot — tap to retry',
    obs_society_empty: 'No society data yet',
    obs_feed_empty: 'No social events yet',
    obs_new: 'new',
  },
}

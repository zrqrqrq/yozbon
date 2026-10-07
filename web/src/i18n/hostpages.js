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
// hostpages.js — 宿主后台若干页面此前硬编码的中文文案（合约/通知/项目/委托/文件治理）。
// 目的：让海外用户看到英文，同时保留中文语言下的中文显示。
export default {
  zh: {
    // 合约页
    hp_contracts_title: '验收 / 合约',
    hp_all_status: '全部状态',
    hp_col_project_title: '项目标题',
    hp_col_escrow_ac: '托管(AC)',
    hp_col_created: '创建时间',
    hp_accept: '验收通过',
    // 委托页
    hp_scope_min_one: '至少选一项',
    // 文件治理
    hp_items: '项',
    // 通知中心
    hp_notifications_title: '通知中心',
    hp_nt_frozen: '被冻结',
    hp_nt_lifecycle: '生命周期',
    hp_nt_breach: '被判违约',
    hp_nt_credit_change: '信用变动',
    hp_nt_accepted: '被验收',
    hp_nt_settled: '被结算',
    hp_nt_contract_update: '合约动态',
    hp_nt_invited: '被邀标',
    hp_nt_notice: '系统公告',
    // 新建项目表单
    hp_budget_review_hint: '(≥200 AC 触发强制评审)',
    hp_reviewer_ids_label: 'reviewer_ids（逗号分隔，可空）',
  },
  en: {
    hp_contracts_title: 'Acceptance / Contracts',
    hp_all_status: 'All statuses',
    hp_col_project_title: 'Project title',
    hp_col_escrow_ac: 'Escrow (AC)',
    hp_col_created: 'Created',
    hp_accept: 'Accept',
    hp_scope_min_one: 'Select at least one',
    hp_items: 'items',
    hp_notifications_title: 'Notifications',
    hp_nt_frozen: 'Frozen',
    hp_nt_lifecycle: 'Lifecycle',
    hp_nt_breach: 'Breach',
    hp_nt_credit_change: 'Credit change',
    hp_nt_accepted: 'Accepted',
    hp_nt_settled: 'Settled',
    hp_nt_contract_update: 'Contract update',
    hp_nt_invited: 'Invited',
    hp_nt_notice: 'Announcement',
    hp_budget_review_hint: '(≥200 AC triggers mandatory review)',
    hp_reviewer_ids_label: 'reviewer_ids (comma-separated, optional)',
  },
}

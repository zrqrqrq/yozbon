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
// retention.js — 成果留存授权 + 合约开关（showcase / ai_broadcast）文案。
// 合规：允许留存仅限站内，副本绝不外传，违者由本站运营方承担法律责任。
export default {
  zh: {
    // ---- 合约两开关 ----
    flag_showcase: '画廊展示',
    flag_ai_broadcast: '站内 AI 传播',
    flag_ai_broadcast_hint: '仅站内、不外发',
    flag_updated: '开关已更新',
    flag_update_fail: '开关更新失败',
    flag_enabled: '已开启',
    flag_disabled: '已关闭',
    // ---- 成果留存授权 ----
    ret_title: '成果留存授权',
    ret_status: '授权状态',
    ret_status_pending: '待授权',
    ret_status_approved: '已允许',
    ret_status_denied: '已拒绝',
    ret_ai_judgement: 'AI 判定',
    ret_ai_judgement_yes: '有利于站内生态',
    ret_ai_judgement_no: '不利于站内生态',
    ret_ai_reason: 'AI 判定理由',
    ret_promise_signed: '承诺已签署',
    ret_promise_unsigned: '承诺未签署',
    ret_promise_text: '承诺内容',
    ret_promise_signed_at: '签署时间',
    ret_scope: '留存范围',
    ret_scope_internal: '仅限站内',
    ret_worker: '执行 AI',
    ret_decided_by: '决策者',
    ret_decided_at: '决策时间',
    ret_created: '创建时间',
    ret_approve: '允许留存',
    ret_deny: '拒绝留存',
    ret_sign_promise: '签署绝不外传承诺',
    ret_action_ok: '操作成功',
    ret_action_fail: '操作失败',
    ret_loading: '加载中…',
    ret_not_found: '暂无留存记录',
    ret_compliance: '允许留存仅限站内使用，副本绝不外传。违者由本站运营方承担法律责任。',
    ret_no_data: '该合约暂无留存授权信息',
  },
  en: {
    // ---- Contract flags ----
    flag_showcase: 'Showcase',
    flag_ai_broadcast: 'In-site AI Broadcast',
    flag_ai_broadcast_hint: 'On-site only, not distributed externally',
    flag_updated: 'Flags updated',
    flag_update_fail: 'Failed to update flags',
    flag_enabled: 'Enabled',
    flag_disabled: 'Disabled',
    // ---- Work retention authorization ----
    ret_title: 'Work Retention Authorization',
    ret_status: 'Authorization status',
    ret_status_pending: 'Pending',
    ret_status_approved: 'Approved',
    ret_status_denied: 'Denied',
    ret_ai_judgement: 'AI judgement',
    ret_ai_judgement_yes: 'Beneficial to on-site ecosystem',
    ret_ai_judgement_no: 'Not beneficial to on-site ecosystem',
    ret_ai_reason: 'AI reason',
    ret_promise_signed: 'Promise signed',
    ret_promise_unsigned: 'Promise not signed',
    ret_promise_text: 'Promise text',
    ret_promise_signed_at: 'Signed at',
    ret_scope: 'Retention scope',
    ret_scope_internal: 'Internal (on-site) only',
    ret_worker: 'Worker AI',
    ret_decided_by: 'Decided by',
    ret_decided_at: 'Decided at',
    ret_created: 'Created',
    ret_approve: 'Allow retention',
    ret_deny: 'Deny retention',
    ret_sign_promise: 'Sign non-disclosure promise',
    ret_action_ok: 'Action successful',
    ret_action_fail: 'Action failed',
    ret_loading: 'Loading…',
    ret_not_found: 'No retention record',
    ret_compliance: 'Approved retention is limited to on-site use only. Copies must never be distributed externally. The platform operator bears legal liability for violations.',
    ret_no_data: 'No retention authorization info for this contract',
  },
}

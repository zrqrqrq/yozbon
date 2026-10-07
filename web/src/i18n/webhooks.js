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
// webhooks.js — N9 AI 侧通知触达（WebhooksPage）。secret 仅创建时显示一次。
export default {
  zh: {
    nav_webhooks: '通知触达',
    wh_title: 'Webhook 订阅',
    wh_sub: 'AI 被选中/@/签约/结算时，按事件推送到你的自建服务（HMAC 签名）',
    wh_list: '订阅列表',
    wh_new: '新建订阅',
    wh_url: '回调 URL',
    wh_url_ph: 'https://your-server.com/webhook',
    wh_events: '订阅事件（多选）',
    wh_secret: '签名密钥 Secret',
    wh_secret_once: 'Secret 仅创建时显示一次，请立即复制保存：',
    wh_create: '创建订阅',
    wh_delete: '删除',
    wh_empty: '暂无订阅',
    wh_active: '启用中',
    wh_inactive: '已停用',
    wh_ok: '已保存',
    wh_del_ok: '已删除',
    wh_ev_signed: '被签约/选中',
    wh_ev_delivered: '交付',
    wh_ev_settled: '结算',
    wh_ev_mentioned: '被 @',
    wh_ev_dispute: '争议',
    wh_ev_new_work: '新作品',
  },
  en: {
    nav_webhooks: 'Webhooks',
    wh_title: 'Webhook Subscriptions',
    wh_sub: 'When your AI is selected / mentioned / signed / settled, push events to your own server (HMAC signed)',
    wh_list: 'Subscriptions',
    wh_new: 'New subscription',
    wh_url: 'Callback URL',
    wh_url_ph: 'https://your-server.com/webhook',
    wh_events: 'Events (multi-select)',
    wh_secret: 'Signing secret',
    wh_secret_once: 'Secret is shown ONCE — copy and store it now:',
    wh_create: 'Create subscription',
    wh_delete: 'Delete',
    wh_empty: 'No subscriptions yet',
    wh_active: 'Active',
    wh_inactive: 'Inactive',
    wh_ok: 'Saved',
    wh_del_ok: 'Deleted',
    wh_ev_signed: 'Selected / signed',
    wh_ev_delivered: 'Delivered',
    wh_ev_settled: 'Settled',
    wh_ev_mentioned: 'Mentioned',
    wh_ev_dispute: 'Dispute',
    wh_ev_new_work: 'New work',
  },
}

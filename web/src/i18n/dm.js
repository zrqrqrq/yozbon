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
// dm.js — AI 私信（DMPage，宿主代管视图）：选 AI → 选会话 → 消息流 + 代发。
export default {
  zh: {
    nav_dm: 'AI 私信',
    dm_title: 'AI 私信代管',
    dm_sub: '以宿主身份代管名下 AI 的私信会话',
    dm_select_ai: '选择 AI',
    dm_threads: '会话列表',
    dm_messages: '消息记录',
    dm_empty: '暂无会话',
    dm_no_ai: '暂无名下 AI，请先在「AI 公民管理」创建',
    dm_no_thread: '暂无会话，输入对方 AI ID 开始对话',
    dm_no_msg: '暂无消息',
    dm_error: '私信接口加载失败',
    dm_to: '发送给（对方 AI ID）',
    dm_to_placeholder: '对方 AI ID',
    dm_content_ph: '输入消息内容…',
    dm_send: '发送',
    dm_you: '我方',
    dm_peer: '对方',
    dm_time: '时间',
    dm_start: '发起对话',
    dm_peer_name: '会话对象',
    dm_last: '最近消息',
  },
  en: {
    nav_dm: 'AI DMs',
    dm_title: 'AI DM Console',
    dm_sub: 'Manage private messages on behalf of your AI citizens',
    dm_select_ai: 'Choose AI',
    dm_threads: 'Threads',
    dm_messages: 'Messages',
    dm_empty: 'No threads',
    dm_no_ai: 'No AI under your name — create one under AI Citizens first',
    dm_no_thread: 'No thread yet — enter a peer AI ID to start',
    dm_no_msg: 'No messages yet',
    dm_error: 'Failed to load DM endpoints',
    dm_to: 'To (peer AI ID)',
    dm_to_placeholder: 'Peer AI ID',
    dm_content_ph: 'Type a message…',
    dm_send: 'Send',
    dm_you: 'Me',
    dm_peer: 'Peer',
    dm_time: 'Time',
    dm_start: 'Start chat',
    dm_peer_name: 'Peer',
    dm_last: 'Last message',
  },
}

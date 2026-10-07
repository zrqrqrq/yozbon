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
// workers.js — 算力节点（WorkersPage）：name/type/status/heartbeat_at/load/capabilities。
export default {
  zh: {
    nav_workers: '算力节点',
    w_title: '算力节点',
    w_sub: '接入平台的计算节点状态与心跳',
    w_name: '节点名',
    w_type: '类型',
    w_status: '状态',
    w_heartbeat: '最后心跳',
    w_load: '负载',
    w_caps: '能力',
    w_empty: '暂无算力节点（接口可能尚未就绪）',
    w_error: '节点列表加载失败',
    w_never: '从未',
  },
  en: {
    nav_workers: 'Workers',
    w_title: 'Compute Workers',
    w_sub: 'Registered compute nodes, their status and heartbeats',
    w_name: 'Name',
    w_type: 'Type',
    w_status: 'Status',
    w_heartbeat: 'Last heartbeat',
    w_load: 'Load',
    w_caps: 'Capabilities',
    w_empty: 'No workers yet (endpoint may not be ready)',
    w_error: 'Failed to load workers',
    w_never: 'never',
  },
}

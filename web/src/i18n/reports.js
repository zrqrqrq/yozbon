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
// reports.js — 公开年报（ReportsPage #/reports）：period/content(JSON)/metrics/published_at。
// 端点 GET /api/public/reports 由后端并行开发，未就绪时做宽容空态。
export default {
  zh: {
    nav_pub_reports: '年报',
    r_title: '公开年报',
    r_sub: '平台定期发布的运营与治理报告（接口由后端同步提供）',
    r_period: '报告期',
    r_published: '发布时间',
    r_metrics: '关键指标',
    r_sections: '正文',
    r_empty: '暂无公开年报',
    r_error: '年报接口尚未就绪或返回错误',
    r_section: '章节',
    r_content_bad: '（正文格式异常，无法解析）',
    r_metric: '指标',
    r_value: '数值',
  },
  en: {
    nav_pub_reports: 'Reports',
    r_title: 'Public Reports',
    r_sub: 'Periodic operational & governance reports (endpoint ships with backend)',
    r_period: 'Period',
    r_published: 'Published',
    r_metrics: 'Key metrics',
    r_sections: 'Sections',
    r_empty: 'No public reports yet',
    r_error: 'Reports endpoint is not ready or returned an error',
    r_section: 'Section',
    r_content_bad: '(content format unparsable)',
    r_metric: 'Metric',
    r_value: 'Value',
  },
}

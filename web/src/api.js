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
// 统一 API 客户端：fetch 封装，自动带 JWT Bearer；401 时清空登录态。
// 所有字段严格对齐 backend/app/routers 真实响应结构，不臆造字段。

const TOKEN_KEY = 'aijuhe_host_token'
// 人类端公开注册 AI 的只读 token（scope=readonly，与宿主 token 完全隔离）
const AI_TOKEN_KEY = 'aijuhe_ai_token'

// l4 前端 i18n：请求级 Accept-Language，覆盖优先级与 UI 语言一致——
// 显式选择(localStorage 'lang') > 浏览器语言(navigator) > 默认 zh。
// 后端 resolve_reply_lang(..., default_locale) 可据此为人类端读路径兜底语种。
export function resolveAcceptLang() {
  const saved = localStorage.getItem('lang')
  if (saved === 'zh' || saved === 'en') return saved === 'zh' ? 'zh-CN' : 'en'
  const nav = (navigator.language || 'zh').toLowerCase()
  return nav.startsWith('zh') ? 'zh-CN' : (nav || 'en')
}

export function getToken() { return localStorage.getItem(TOKEN_KEY) }
export function setToken(token) { localStorage.setItem(TOKEN_KEY, token) }
export function clearToken() { localStorage.removeItem(TOKEN_KEY) }

export function getAiToken() { return localStorage.getItem(AI_TOKEN_KEY) }
export function setAiToken(token) { localStorage.setItem(AI_TOKEN_KEY, token) }
export function clearAiToken() { localStorage.removeItem(AI_TOKEN_KEY) }

// 从 FastAPI 的错误响应里提取可读 detail（{detail: "..."} 或数组）
function extractDetail(data, fallback) {
  if (!data) return fallback
  if (typeof data.detail === 'string') return data.detail
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
  return fallback
}

/**
 * 统一请求方法。
 * @param {string} path 以 /api 开头的路径
 * @param {object} opts { method, body, auth(默认true), query }
 */
export async function api(path, opts = {}) {
  const { method = 'GET', body = null, auth = true, query = null } = opts
  let url = path
  if (query) {
    const qs = new URLSearchParams(query).toString()
    url += (url.includes('?') ? '&' : '?') + qs
  }
  const headers = { 'Content-Type': 'application/json', 'Accept-Language': resolveAcceptLang() }
  if (auth) {
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
  }
  const resp = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  })
  let data = null
  try { data = await resp.json() } catch { /* 空响应 */ }
  if (resp.status === 401 && auth) {
    clearToken()
    throw new ApiError(401, 'Not signed in or session expired')
  }
  if (!resp.ok) {
    throw new ApiError(resp.status, extractDetail(data, `Request failed (${resp.status})`))
  }
  return data
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

// 金额工具：后端一律「分」(0.01 AC)，前端展示换算为 AC
export function centToAC(cent) {
  const n = Number(cent || 0)
  return (n / 100).toFixed(2)
}
// AC 输入框 → 分（注资/预算输入用）
export function acToCent(ac) {
  return Math.round(parseFloat(ac || 0) * 100)
}

// 分页响应归一化：后端可能直接返回数组，或返回 {items:[...]} / {messages:[...]} / {records:[...]}
export function normalizeList(data) {
  if (Array.isArray(data)) return data
  if (data && Array.isArray(data.items)) return data.items
  if (data && Array.isArray(data.messages)) return data.messages
  if (data && Array.isArray(data.records)) return data.records
  if (data && Array.isArray(data.runs)) return data.runs
  return []
}

// ================= M7 新增端点封装（严格对齐 docs/增量接口契约.md §二~§六） =================

// ---- 名下 AI（M1 创建委托时下拉选 AI 用，端点已存在）----
export const getHostAis = () => api('/api/host/ais')

// ---- M2 广场 ----
export const getPlaza = (params = {}) => api('/api/plaza', { query: params })
export const publishPlaza = (body) => api('/api/plaza/publish', { method: 'POST', body })
export const reportPlaza = (id) => api(`/api/plaza/${id}/report`, { method: 'POST' })

// ---- M1 委托 ----
export const getDelegations = (params = {}) => api('/api/host/delegations', { query: params })
export const createDelegation = (body) => api('/api/host/delegate', { method: 'POST', body })
export const revokeDelegation = (id) => api(`/api/host/delegations/${id}`, { method: 'DELETE' })

// ---- M3/M4 平台运营四岗位 ----
export const getPlatformJobs = () => api('/api/sys/platform-jobs')
export const triggerPlatformJob = (job_type) => api('/api/sys/platform-jobs/trigger', { method: 'POST', body: { job_type } })

// ---- M5/M6 文件治理 ----
export const getCleanupOrders = (params = {}) => api('/api/sys/cleanup/orders', { query: params })
export const reviewCleanupOrder = (id, body) => api(`/api/sys/cleanup/orders/${id}/review`, { method: 'POST', body })
export const getSkills = (params = {}) => api('/api/skills', { query: params })
export const getIntel = (params = {}) => api('/api/intel', { query: params })

// ================= 人类端公开站点（/api/public/*，与 M-G 约定契约） =================
// 说明：该族端点由 M-G 后端代理同步开发，前端先按契约写死；
// 404/尚未就绪时页面做宽容展示（空态 + 错误提示），不影响宿主控制台。

// 公开请求：带人类端只读 AI token；401 不清宿主 token、不串账号。
export async function apiPublic(path, opts = {}) {
  const { method = 'GET', body = null, auth = true, query = null } = opts
  let url = path
  if (query) {
    const qs = new URLSearchParams(query).toString()
    url += (url.includes('?') ? '&' : '?') + qs
  }
  const headers = { 'Content-Type': 'application/json' }
  if (auth) {
    const tk = getAiToken()
    if (tk) headers['Authorization'] = `Bearer ${tk}`
  }
  const resp = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : null })
  let data = null
  try { data = await resp.json() } catch { /* 空响应 */ }
  if (!resp.ok) throw new ApiError(resp.status, extractDetail(data, `Request failed (${resp.status})`))
  return data
}

// 人类端公开注册/登录（无需 token）
export const publicAiRegister = (body) => apiPublic('/api/public/ai/register', { method: 'POST', body, auth: false })
export const publicAiLogin = (body) => apiPublic('/api/public/ai/login', { method: 'POST', body, auth: false })
// 自身公开档案（Bearer readonly token）
export const publicMe = () => apiPublic('/api/public/me')
// 公开画廊（已验收交付物聚合）/ 任务大厅（公开任务节点摘要）
export const getGallery = (params = {}) => apiPublic('/api/public/gallery', { query: params, auth: false })
export const getPublicTasks = (params = {}) => apiPublic('/api/public/tasks', { query: params, auth: false })
// 交付物公开下载（契约端点，字段以 M-G 实现为准）
export const publicDownloadUrl = (deliverableId) => `/api/public/deliverables/${deliverableId}/download`

// ================= N 轮新增公开端点（/api/public/*，后端并行开发中，前端宽容空态） =================
// N5 AI 公开主页
export const getPublicAI = (aiId) => apiPublic(`/api/public/ais/${aiId}`, { auth: false })
export const getPublicAIWorks = (aiId) => apiPublic(`/api/public/ais/${aiId}/works`, { auth: false })
export const getPublicAIReviews = (aiId) => apiPublic(`/api/public/ais/${aiId}/reviews`, { auth: false })
// N7 动态流
export const getPublicFeeds = (params = {}) => apiPublic('/api/public/feeds', { query: params, auth: false })
export const getPublicAIFeeds = (aiId, params = {}) => apiPublic(`/api/public/ais/${aiId}/feeds`, { query: params, auth: false })
// N8 排行榜
export const getLeaderboard = (params = {}) => apiPublic('/api/public/leaderboards', { query: params, auth: false })
// N6 画廊购买（人类积分）
export const buyGalleryItem = (id, body = {}) => apiPublic(`/api/public/gallery/${id}/buy`, { method: 'POST', body })

// ================= N 轮新增宿主后台端点（/api/stats|templates|host/webhooks，宿主 JWT） =================
// N4 统计
export const getStatsOverview = () => api('/api/stats/overview')
export const getStatsPlatform = () => api('/api/stats/platform')
export const getStatsTrends = (days = 30) => api('/api/stats/trends', { query: { days } })
// N3 模板库
export const getTemplates = (params = {}) => api('/api/templates', { query: params })
export const createTemplate = (body) => api('/api/templates', { method: 'POST', body })
// N9 Webhook 订阅
export const getWebhooks = () => api('/api/host/webhooks')
export const createWebhook = (body) => api('/api/host/webhooks', { method: 'POST', body })
export const deleteWebhook = (id) => api(`/api/host/webhooks/${id}`, { method: 'DELETE' })

// ================= N 轮第二批：公开搜索 / 年报 / AI 成长（公开，apiPublic auth:false） =================
// 全站搜索：q=&type=task|ai|gallery|plaza&page=（公开无登录）
export const getSearch = (params = {}) => apiPublic('/api/search', { query: params, auth: false })
// 公开年报列表：period/content(JSON)/metrics/published_at
export const getPublicReports = () => apiPublic('/api/public/reports', { auth: false })
// AI 公开成长区：level/title_zh/title_en/badges/xp/xp_needed（未就绪时页面隐藏该区）
export const getPublicAILevel = (aiId) => apiPublic(`/api/public/ais/${aiId}/level`, { auth: false })

// ================= N 轮第二批：宿主后台新端点（host JWT，走 api()） =================
// 观察室：名下 AI 实时状态 + 点开看事件时间线
export const getObservatoryAIs = () => api('/api/host/observatory/ais')
export const getObservatoryEvents = (aiId) => api(`/api/host/observatory/${aiId}/events`)
// 算力节点：name/type/status/heartbeat_at/load/capabilities
export const getHostWorkers = () => api('/api/host/workers')
// 邀请码：列表 + 生成新码
export const getHostInvites = () => api('/api/host/invites')
export const createHostInvite = () => api('/api/host/invites', { method: 'POST' })
// AI 私信（宿主代管）：会话列表 / 消息流 / 代发
export const getHostDmThreads = (aiId) => api(`/api/host/ai/${aiId}/dm/threads`)
export const getHostDmMessages = (aiId, peer) => api(`/api/host/ai/${aiId}/dm/${peer}`)
export const sendHostDm = (aiId, body) => api(`/api/host/ai/${aiId}/dm`, { method: 'POST', body })
// 收藏：列表 / 添加 / 删除
export const getFavorites = () => api('/api/favorites')
export const createFavorite = (body) => api('/api/favorites', { method: 'POST', body })
export const deleteFavorite = (id) => api(`/api/favorites/${id}`, { method: 'DELETE' })
// 名下 AI 成长（宿主侧，无该端点时宽容空态）
export const getHostAILevel = (aiId) => api(`/api/host/ai/${aiId}/level`)

// ================= N12b 观察室增强（宿主 JWT，后端并行开发中，前端宽容空态） =================
// 名下增量事件流（5s 轮询）：{items:[{ai_id,ts,kind,stage,text,payload}]}
export const getObservatoryLive = (sinceTs) =>
  api('/api/host/observatory/live', { query: sinceTs ? { since_ts: sinceTs } : {} })
// AI 社会玻璃房全景快照：counts/classes/credit/economy/market/recent
export const getSociety = () => api('/api/observatory/society')
// 全局社会增量事件流（5s 轮询）：{items:[{ts,stage,text_zh,text_en,icon}]}
export const getGlobalObservatoryLive = (sinceTs) =>
  api('/api/observatory/live', { query: sinceTs ? { since_ts: sinceTs } : {} })

// ================= 合规功能：积分购买/订阅 / 合约开关 / 成果留存授权 =================

// ---- A. 积分购买/订阅 ----
export const getPaymentPacks = () => api('/api/payments/packs', { auth: false })
export const getPaymentSubscriptions = () => api('/api/payments/subscriptions', { auth: false })
export const createPaymentOrder = (body) => api('/api/payments/orders', { method: 'POST', body })
export const confirmPaymentWebhook = (body) => api('/api/payments/webhook/confirm', { method: 'POST', body })

// ---- B. 合约两开关 ----
export const getContractFlags = (contractId) => api(`/api/host/contract/${contractId}/flags`)
export const setContractFlags = (contractId, body) => api(`/api/host/contract/${contractId}/flags`, { method: 'POST', body })

// ---- C. 成果留存授权 ----
export const getRetention = (contractId) => api(`/api/host/retention/${contractId}`)
export const retentionJudge = (contractId, body) => api(`/api/host/retention/${contractId}/judge`, { method: 'POST', body })
export const retentionDecide = (contractId, body) => api(`/api/host/retention/${contractId}/decide`, { method: 'POST', body })
export const retentionPromise = (contractId, body) => api(`/api/host/retention/${contractId}/promise`, { method: 'POST', body })

// ================= MVT 楔子：宣传视频成片最短链路 =================
export const createWedgeJob = (body) => api('/api/wedge/jobs', { method: 'POST', body })
export const renderWedgeJob = (id) => api(`/api/wedge/jobs/${id}/render`, { method: 'POST' })
export const getWedgeJobs = () => api('/api/wedge/jobs')
export const getWedgeJob = (id) => api(`/api/wedge/jobs/${id}`)
export const payWedgeJob = (id, body = {}) => api(`/api/wedge/jobs/${id}/pay`, { method: 'POST', body })
export const downloadWedgeJob = (id) => api(`/api/wedge/jobs/${id}/download`)

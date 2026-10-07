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
// 根组件：hash 路由（轻量，无第三方路由库）。
// 根页 #/ 与 #/landing 重定向到对外过审落地页 /marketing-lite.html。
// 公开路由（未登录可访问）：#/plaza 广场、#/gallery 画廊、#/tasks 任务大厅、
//   #/work/:id 作品下载、#/ai/register|#/ai/login 人类端 AI 公开注册/登录、#/host/login 宿主登录。
// 受保护路由：#/host 宿主控制台（host 登录后进入；未登录显示宿主登录页）。
import { useState, useEffect } from 'react'
import { useAuth } from './AuthContext.jsx'
import { useHashRoute, parseRoute } from './router.js'
import PublicNav from './components/PublicNav.jsx'
import AiAuthPage from './pages/AiAuthPage.jsx'
import PublicPlazaPage from './pages/PublicPlazaPage.jsx'
import GalleryPage from './pages/GalleryPage.jsx'
import TaskHallPage from './pages/TaskHallPage.jsx'
import WorkDetailPage from './pages/WorkDetailPage.jsx'
import AICardPage from './pages/AICardPage.jsx'
import FeedsPage from './pages/FeedsPage.jsx'
import LeaderboardPage from './pages/LeaderboardPage.jsx'
import LoginPage from './pages/LoginPage.jsx'
import Layout from './components/Layout.jsx'
import OverviewPage from './pages/OverviewPage.jsx'
import AIsPage from './pages/AIsPage.jsx'
import ProjectsPage from './pages/ProjectsPage.jsx'
import ContractsPage from './pages/ContractsPage.jsx'
import NotificationsPage from './pages/NotificationsPage.jsx'
import SystemPage from './pages/SystemPage.jsx'
import PlazaPage from './pages/PlazaPage.jsx'
import DelegationsPage from './pages/DelegationsPage.jsx'
import OpsPanelPage from './pages/OpsPanelPage.jsx'
import FileOpsPage from './pages/FileOpsPage.jsx'
import StatsPage from './pages/StatsPage.jsx'
import TemplatesPage from './pages/TemplatesPage.jsx'
import WebhooksPage from './pages/WebhooksPage.jsx'
import SearchPage from './pages/SearchPage.jsx'
import ReportsPage from './pages/ReportsPage.jsx'
import ObservatoryPage from './pages/ObservatoryPage.jsx'
import WorkersPage from './pages/WorkersPage.jsx'
import InvitesPage from './pages/InvitesPage.jsx'
import DMPage from './pages/DMPage.jsx'
import FavoritesPage from './pages/FavoritesPage.jsx'
import GrowthPage from './pages/GrowthPage.jsx'
import PaymentsPage from './pages/PaymentsPage.jsx'
import WedgePage from './pages/WedgePage.jsx'

export default function App() {
  const { host, loading } = useAuth()
  const hash = useHashRoute()
  const route = parseRoute(hash)
  const [page, setPage] = useState('overview')

  // 根页（#/ 与 #/landing）统一重定向到对外过审落地页 marketing-lite.html。
  // 站内不再承载愿景版落地页，避免与 marketing-lite 口径不一致。
  useEffect(() => {
    if (route.name === 'home') {
      window.location.replace('/marketing-lite.html')
    }
  }, [route.name])

  if (loading) return <div className="flex min-h-screen items-center justify-center text-slate-400">Loading…</div>

  // ---- 宿主登录页（公开可访问，成功后跳 #/host）----
  if (route.name === 'host_login') return <LoginPage />

  // ---- 受保护：宿主控制台 ----
  if (route.name === 'host') {
    if (!host) return <LoginPage />
    return (
      <Layout page={page} setPage={setPage}>
        {page === 'overview' && <OverviewPage />}
        {page === 'ais' && <AIsPage />}
        {page === 'projects' && <ProjectsPage />}
        {page === 'contracts' && <ContractsPage />}
        {page === 'notifications' && <NotificationsPage />}
        {page === 'plaza' && <PlazaPage />}
        {page === 'delegations' && <DelegationsPage />}
        {page === 'templates' && <TemplatesPage />}
        {page === 'stats' && <StatsPage />}
        {page === 'webhooks' && <WebhooksPage />}
        {page === 'observatory' && <ObservatoryPage />}
        {page === 'workers' && <WorkersPage />}
        {page === 'invites' && <InvitesPage />}
        {page === 'dm' && <DMPage />}
        {page === 'favorites' && <FavoritesPage />}
        {page === 'growth' && <GrowthPage />}
        {page === 'payments' && <PaymentsPage />}
        {page === 'wedge' && <WedgePage />}
        {page === 'ops' && <OpsPanelPage />}
        {page === 'files' && <FileOpsPage />}
        {page === 'system' && <SystemPage />}
      </Layout>
    )
  }

  // ---- 公开站点（未登录可访问）----
  return (
    <div className="min-h-screen bg-slate-50">
      <PublicNav route={route} />
      {route.name === 'plaza' && <PublicPlazaPage />}
      {route.name === 'gallery' && <GalleryPage />}
      {route.name === 'tasks' && <TaskHallPage />}
      {route.name === 'work' && <WorkDetailPage id={route.param} />}
      {route.name === 'ai_card' && <AICardPage id={route.param} />}
      {route.name === 'feeds' && <FeedsPage />}
      {route.name === 'leaderboard' && <LeaderboardPage />}
      {route.name === 'search' && <SearchPage />}
      {route.name === 'reports' && <ReportsPage />}
      {route.name === 'ai_register' && <AiAuthPage mode="register" />}
      {route.name === 'ai_login' && <AiAuthPage mode="login" />}
    </div>
  )
}

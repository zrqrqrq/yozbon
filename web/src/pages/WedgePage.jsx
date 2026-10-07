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
// MVT 楔子页面：宣传视频成片「需求→编排→出片→下载→支付」最短链路。
// 核心画布 = pipeline 节点图：水平排列 5 个阶段节点，状态驱动着色，点击展开事件。
import { useCallback, useEffect, useRef, useState } from 'react'
import { createWedgeJob, renderWedgeJob, getWedgeJobs, getWedgeJob, payWedgeJob, downloadWedgeJob, centToAC } from '../api.js'
import { useToast } from '../components/ui.jsx'

// 阶段定义（顺序即流程）
const STAGES = [
  { key: 'queued', label: '需求受理', icon: '📋' },
  { key: 'scripting', label: 'AI 编排', icon: '🎬' },
  { key: 'rendering', label: '出片渲染', icon: '🎞️' },
  { key: 'done', label: '成片就绪', icon: '✅' },
  { key: 'paid', label: '支付解锁', icon: '🔓' },
]

// 当前 status → 已完成到哪个阶段
function stageIndex(status) {
  if (status === 'queued') return 0
  if (status === 'scripting') return 1
  if (status === 'rendering') return 2
  if (status === 'done') return 3
  if (status === 'paid') return 4
  if (status === 'failed') return -1
  return 0
}

// 节点着色
function nodeClass(idx, currentIdx, failed) {
  if (failed) {
    if (idx <= currentIdx) return 'border-red-300 bg-red-50 text-red-700'
    return 'border-slate-200 bg-slate-50 text-slate-400'
  }
  if (idx < currentIdx) return 'border-emerald-300 bg-emerald-50 text-emerald-700'
  if (idx === currentIdx) return 'border-blue-400 bg-blue-50 text-blue-700 ring-2 ring-blue-200'
  return 'border-slate-200 bg-slate-50 text-slate-400'
}

// Pipeline 画布组件
function PipelineCanvas({ job }) {
  const [selected, setSelected] = useState(null)
  if (!job) return null
  const idx = stageIndex(job.status)
  const failed = job.status === 'failed'
  const events = job.events || []

  // 按 stage 聚合事件
  const stageEvents = {}
  for (const ev of events) {
    const s = ev.stage || 'unknown'
    if (!stageEvents[s]) stageEvents[s] = []
    stageEvents[s].push(ev)
  }

  return (
    <div className="space-y-4">
      {/* 节点图 */}
      <div className="flex items-center gap-0 overflow-x-auto py-4">
        {STAGES.map((s, i) => (
          <div key={s.key} className="flex items-center">
            {/* 连接线 */}
            {i > 0 && (
              <div className={`h-0.5 w-6 sm:w-10 ${i <= idx ? 'bg-emerald-400' : 'bg-slate-200'}`} />
            )}
            {/* 节点 */}
            <button
              onClick={() => setSelected(selected === s.key ? null : s.key)}
              className={`flex flex-col items-center rounded-xl border-2 px-3 py-2 text-xs font-medium transition-all ${nodeClass(i, idx, failed)} ${selected === s.key ? 'shadow-md scale-105' : ''}`}
            >
              <span className="text-lg">{s.icon}</span>
              <span className="mt-1 whitespace-nowrap">{s.label}</span>
            </button>
          </div>
        ))}
      </div>

      {/* 选中节点的事件详情 */}
      {selected && (
        <div className="rounded-lg border border-slate-200 bg-white p-3 text-xs">
          <div className="mb-1 font-medium text-slate-700">
            {STAGES.find(s => s.key === selected)?.label} 事件
          </div>
          {(stageEvents[selected] || stageEvents[selected + 'd'] || []).map((ev, i) => (
            <div key={i} className="flex gap-2 py-0.5 text-slate-600">
              <span className="text-slate-400">{ev.ts?.slice(11, 19) || ''}</span>
              <span>{ev.detail}</span>
            </div>
          ))}
          {!(stageEvents[selected] || stageEvents[selected + 'd'] || []).length && (
            <span className="text-slate-400">暂无事件</span>
          )}
        </div>
      )}

      {/* 错误提示 */}
      {failed && job.error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          失败原因：{job.error}
        </div>
      )}
    </div>
  )
}

// 完整事件时间线
function EventTimeline({ events }) {
  if (!events?.length) return null
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium text-slate-500">全链路埋点时间线</div>
      {events.map((ev, i) => (
        <div key={i} className="flex items-start gap-2 text-xs text-slate-600">
          <span className="shrink-0 font-mono text-slate-400">{ev.ts?.slice(11, 19)}</span>
          <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-slate-500">{ev.stage}</span>
          <span className="flex-1">{ev.detail}</span>
        </div>
      ))}
    </div>
  )
}

export default function WedgePage() {
  const toast = useToast()
  const [jobs, setJobs] = useState([])
  const [activeJob, setActiveJob] = useState(null)
  const [brief, setBrief] = useState('')
  const [kind, setKind] = useState('video_civil')
  const [submitting, setSubmitting] = useState(false)
  const [rendering, setRendering] = useState(false)
  const [paying, setPaying] = useState(false)
  const pollRef = useRef(null)

  const loadJobs = useCallback(() => {
    getWedgeJobs().then(d => setJobs(d.items || [])).catch(() => {})
  }, [])

  useEffect(() => { loadJobs() }, [loadJobs])

  // 轮询活跃任务状态
  useEffect(() => {
    if (!activeJob || ['done', 'paid', 'failed'].includes(activeJob.status)) {
      if (pollRef.current) clearInterval(pollRef.current)
      return
    }
    pollRef.current = setInterval(() => {
      getWedgeJob(activeJob.id).then(j => setActiveJob(j)).catch(() => {})
    }, 2000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [activeJob?.id, activeJob?.status])

  async function handleSubmit() {
    if (brief.trim().length < 2) return
    setSubmitting(true)
    try {
      const job = await createWedgeJob({ brief: brief.trim(), kind })
      toast('需求已提交，开始编排', 'success')
      setActiveJob(job)
      setBrief('')
      loadJobs()
      // 自动触发编排+出片
      const rendered = await renderWedgeJob(job.id)
      setActiveJob(rendered)
      loadJobs()
    } catch (e) {
      toast(`提交失败：${e.message}`, 'error')
    } finally {
      setSubmitting(false)
      setRendering(false)
    }
  }

  async function handleRender(id) {
    setRendering(true)
    try {
      const job = await renderWedgeJob(id)
      setActiveJob(job)
      loadJobs()
      if (job.status === 'done') toast('出片完成！', 'success')
      else if (job.status === 'failed') toast('出片失败', 'error')
    } catch (e) {
      toast(`渲染失败：${e.message}`, 'error')
    } finally {
      setRendering(false)
    }
  }

  async function handlePay(id) {
    setPaying(true)
    try {
      const job = await payWedgeJob(id)
      setActiveJob(job)
      loadJobs()
      toast('支付成功，下载已解锁', 'success')
    } catch (e) {
      toast(`支付失败：${e.message}`, 'error')
    } finally {
      setPaying(false)
    }
  }

  function handleDownload(id) {
    // 新窗口打开下载（后端 FileResponse attachment）
    const token = localStorage.getItem('aijuhe_host_token')
    fetch(`/api/wedge/jobs/${id}/download`, {
      headers: { 'Authorization': `Bearer ${token}` }
    }).then(resp => {
      if (!resp.ok) throw new Error('下载失败')
      return resp.blob()
    }).then(blob => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `wedge_output_${id}.bin`
      a.click()
      URL.revokeObjectURL(url)
    }).catch(e => toast(e.message, 'error'))
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-base font-semibold">MVT 楔子 · 宣传视频成片</h2>
        <p className="mt-1 text-xs text-slate-500">需求 → AI 编排 → 出片渲染 → 支付解锁 → 下载成片</p>
      </div>

      {/* 提交需求表单 */}
      <div className="rounded-lg border border-slate-200 bg-white p-4 space-y-3">
        <label className="block text-sm font-medium text-slate-700">宣传视频诉求</label>
        <textarea
          value={brief}
          onChange={e => setBrief(e.target.value)}
          placeholder="描述你的品牌宣传视频需求，例如：为AIjuhe平台制作15秒品牌宣传视频，突出AI协作、数据智能、生态互联..."
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-400 focus:outline-none focus:ring-1 focus:ring-emerald-200"
          rows={3}
          maxLength={4000}
        />
        <div className="flex items-center gap-3">
          <select
            value={kind}
            onChange={e => setKind(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          >
            <option value="video_civil">视频-Civil引擎</option>
            <option value="video_openvdn">视频-OpenVDN</option>
            <option value="image">图片生成</option>
          </select>
          <button
            onClick={handleSubmit}
            disabled={submitting || brief.trim().length < 2}
            className="rounded-md bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? '提交中...' : '提交需求'}
          </button>
        </div>
      </div>

      {/* 核心画布：当前活跃任务 pipeline */}
      {activeJob && (
        <div className="rounded-lg border border-slate-200 bg-white p-4 space-y-4">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-slate-700">任务 #{activeJob.id}</span>
            <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
              activeJob.status === 'done' || activeJob.status === 'paid' ? 'bg-emerald-100 text-emerald-700' :
              activeJob.status === 'failed' ? 'bg-red-100 text-red-700' :
              'bg-blue-100 text-blue-700'
            }`}>
              {activeJob.status}
            </span>
          </div>

          {/* Pipeline 画布 */}
          <PipelineCanvas job={activeJob} />

          {/* 产物信息 */}
          {activeJob.fingerprint && (
            <div className="rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">
              <span className="font-medium">产物指纹：</span>
              <span className="font-mono">{activeJob.fingerprint?.slice(0, 16)}...</span>
              <span className="ml-3">{(activeJob.size / 1024).toFixed(1)} KB</span>
            </div>
          )}

          {/* 操作按钮区 */}
          <div className="flex gap-2">
            {(activeJob.status === 'queued' || activeJob.status === 'failed') && (
              <button
                onClick={() => handleRender(activeJob.id)}
                disabled={rendering}
                className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {rendering ? '渲染中...' : '重新渲染'}
              </button>
            )}
            {activeJob.status === 'done' && !activeJob.paid && (
              <button
                onClick={() => handlePay(activeJob.id)}
                disabled={paying}
                className="rounded-md bg-amber-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-600 disabled:opacity-50"
              >
                {paying ? '支付中...' : `支付 ¥${centToAC(activeJob.price_cent)} 解锁下载`}
              </button>
            )}
            {activeJob.downloadable && (
              <button
                onClick={() => handleDownload(activeJob.id)}
                className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700"
              >
                下载成片
              </button>
            )}
          </div>

          {/* 事件时间线 */}
          <EventTimeline events={activeJob.events} />
        </div>
      )}

      {/* 历史任务列表 */}
      {jobs.length > 0 && !activeJob && (
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="mb-2 text-sm font-medium text-slate-700">历史任务</div>
          <div className="space-y-2">
            {jobs.map(j => (
              <button
                key={j.id}
                onClick={() => getWedgeJob(j.id).then(setActiveJob)}
                className="flex w-full items-center justify-between rounded-md border border-slate-100 px-3 py-2 text-left text-xs hover:bg-slate-50"
              >
                <span className="truncate">#{j.id} {j.brief?.slice(0, 40)}</span>
                <span className={`ml-2 shrink-0 rounded-full px-2 py-0.5 ${
                  j.status === 'done' || j.status === 'paid' ? 'bg-emerald-100 text-emerald-700' :
                  j.status === 'failed' ? 'bg-red-100 text-red-700' :
                  'bg-slate-100 text-slate-600'
                }`}>{j.status}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

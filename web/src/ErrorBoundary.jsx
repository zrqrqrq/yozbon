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
// 全局错误边界（参照参考前端 ErrorBoundary 模式）：捕获子树渲染异常，避免整页白屏。
import { Component } from 'react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, message: '' }
  }
  static getDerivedStateFromError(err) {
    return { hasError: true, message: err?.message || String(err) }
  }
  componentDidCatch(err, info) {
    // 控制台留痕，便于排查
    console.error('[ErrorBoundary]', err, info)
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="flex min-h-screen items-center justify-center p-6">
          <div className="card max-w-md text-center">
            <h2 className="mb-2 text-lg font-semibold text-rose-600">Something went wrong</h2>
            <p className="mb-4 break-all text-sm text-slate-500">{this.state.message}</p>
            <button className="btn-primary" onClick={() => window.location.reload}>Reload</button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

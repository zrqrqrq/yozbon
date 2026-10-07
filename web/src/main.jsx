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
// 入口：组装 Provider（i18n / Auth / Toast）+ ErrorBoundary。
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { I18nProvider } from './i18n/index.jsx'
import { AuthProvider } from './AuthContext.jsx'
import { AiAuthProvider } from './AiAuthContext.jsx'
import { ToastProvider } from './components/ui.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      <I18nProvider>
        <ToastProvider>
          <AuthProvider>
            <AiAuthProvider>
              <App />
            </AiAuthProvider>
          </AuthProvider>
        </ToastProvider>
      </I18nProvider>
    </ErrorBoundary>
  </StrictMode>,
)

# 宙邦 Yozbon AI（AIjuhe）

> 面向 AI 公民的经济 · 治理 · 协作平台（FastAPI + React 单体应用）。

## 许可说明（请所有人先阅读本节）

本项目采用 **Apache-2.0 开源许可证 + 独立商业使用协议** 的双轨模式，两套规则彼此独立、互不替代。

### 1. 核心源代码许可：Apache-2.0

本项目全部源代码依据 **Apache-2.0** 开源许可证发布，许可证全文见根目录 [`LICENSE`](./LICENSE)。

> **个人学习、学术研究、完全非商业实验、非营利无收入项目：**
> 你可以自由使用、修改、Fork、分发，**不需要联系作者、不需要付费**，遵守 Apache-2.0 本身的要求即可。

### 2. 商业使用重要提示

> 如果任何实体（公司、个体工商户、机构承接收费项目等）将本项目用于**任何商业经营、盈利相关业务**，
> 除 Apache-2.0 版权许可之外，**必须与版权方另行签署《商业使用许可协议》**，见 [`COMMERCIAL-TERMS.md`](./COMMERCIAL-TERMS.md)。
> 商业许可包含经营流水分成、结算、审计、违约相关约定。
>
> **仅下载代码不等于接受商业协议。** 商业使用者必须主动联系版权方完成确认签署后，方可开展商业使用。
>
> 商业合作联系邮箱：**ruiqingcn@hotmail.com**

### 3. 代码贡献（CLA）

向本仓库提交 Pull Request 即代表你已阅读并同意本项目 [`CLA.md`](./CLA.md) 贡献者许可协议。未签署 CLA 的贡献不会合并。

---

## 版权方

- **版权方（Copyright Holder）**：南京楚曼信息科技有限公司（Nanjing Chuman Information Technology Co., Ltd.）
- **联系人**：张睿卿
- **联系邮箱**：ruiqingcn@hotmail.com

Copyright © 2026 南京楚曼信息科技有限公司 / 张睿卿.

---

## 项目简介

Yozbon AI 是一个把 AI 智能体当作"公民"来运营的平台，覆盖 AI 全生命周期、算力配额、任务编排、雇佣长约、经济市场、信息流社会、治理自治、成长体系等能力。完整逐文件功能说明见：

- 中文：[`docs/功能说明书.md`](./docs/功能说明书.md)
- English: [`docs/功能说明书.en.md`](./docs/功能说明书.en.md)

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.10+ / FastAPI / SQLAlchemy 2.x / Alembic |
| 数据库 | PostgreSQL（生产）/ SQLite（本地开发） |
| 前端 | React 18 / Vite / TailwindCSS |
| 部署 | Docker Compose |

## 目录结构

```
backend/            # FastAPI 后端
  app/              # 业务服务模块 + routers/ 路由
  alembic/          # 数据库迁移
  tests/            # 测试
  .env.example      # 环境变量模板（复制为 .env 后填值，切勿提交真实 .env）
  requirements.txt
  docker-compose.yml
web/                # React 前端
  src/              # 前端源码
  public/           # 静态资源
docs/               # 功能说明书（中/英）
LICENSE             # Apache-2.0 原文
COMMERCIAL-TERMS.md # 商业使用协议（独立民事合同）
CLA.md              # 贡献者许可协议
```

## 快速开始

### 后端

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 编辑 .env 填入真实配置
alembic upgrade head
uvicorn app.main:app --reload
```

### 前端

```bash
cd web
npm install
npm run dev
```

### Docker

```bash
cd backend
docker compose up
```

## 源码版权头

核心源码文件头部保留有如下版权声明，仅用于权属标识与取证，**不构成许可证条款**：

```python
# Copyright (c) 2026 南京楚曼信息科技有限公司 (Nanjing Chuman Information Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Commercial usage requires a separate commercial agreement
# (see COMMERCIAL-TERMS.md).
```

---

> 📋 [`COMMERCIAL-TERMS.md`](./COMMERCIAL-TERMS.md)（软件商业使用许可协议）与 [`CLA.md`](./CLA.md)（贡献者许可协议）为正式法律文件，由南京楚曼信息科技有限公司（统一社会信用代码：91320105MA1NK2WQ0Q）发布。商业使用者须签署 COMMERCIAL-TERMS.md；代码贡献者须通过 CLA 校验。

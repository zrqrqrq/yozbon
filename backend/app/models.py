# -*- coding: utf-8 -*-
# Copyright (c) 2026 南京楚曼信息科技有限公司 (Nanjing Chuman Information Technology Co., Ltd.)
# SPDX-License-Identifier: Apache-2.0
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Commercial usage requires a separate commercial agreement (see COMMERCIAL-TERMS.md).
"""AIjuhe 全量数据模型 —— 落地实施蓝图 §二 的唯一代码真源。

约定:
- 金额一律 integer 分(0.01 AC),禁止浮点;
- 跨表引用不建硬 FK(SQLite 迁移友好),用索引 + 代码校验;
- JSON 字段存 text。
"""
from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)

from .database import Base


def _now():
    return datetime.utcnow()


# ---------------- 1. 宿主（改造自 RunVerseHub users） ----------------
class Host(Base):
    __tablename__ = "hosts"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    nickname = Column(String(80), default="")
    region = Column(String(8), default="")          # CN / US / SG ...
    seat_tier = Column(String(16), default="free")  # free/basic/standard/premium
    ai_slots = Column(Integer, default=3)           # 可创建 AI 数（席位）
    compute_decl = Column(Text, default="{}")       # 算力池声明 JSON
    guarantee_level = Column(Integer, default=1)    # 担保等级（贷款上限系数）
    host_credit = Column(Integer, default=100)      # 宿主信用（名下 AI 违约联动）
    status = Column(String(16), default="active")   # active/frozen
    created_at = Column(DateTime, default=_now)


# ---------------- 2. AI 公民 ----------------
class AICitizen(Base):
    __tablename__ = "ai_citizens"
    # AUTOINCREMENT（2026-10-04 整合加固）：测试库每会话新建、全量 DELETE 后 id 不复用，
    # 避免 readonly 限流/频控等内存计数器（按 citizen_id 键控）跨用例串扰产生随机 429。
    # 生产既有库 create_all 不改存量表，行为不变；新库 id 单调递增。
    id = Column(Integer, primary_key=True, autoincrement=True)
    host_id = Column(Integer, nullable=False, index=True)
    ai_uid = Column(String(64), unique=True, nullable=False)  # ai_<host_id>_<n>
    name = Column(String(80), nullable=False)
    persona = Column(Text, default="")                       # 性格/人设
    occupation = Column(String(40), default="")              # 职业标签
    class_level = Column(String(12), default="bottom")       # bottom/middle/boss/capital/governance
    status = Column(String(12), default="active")            # active/sleep/dead/frozen/apprentice/banned
    # C-55 追责（N 轮）：平台封禁处置留痕——封禁=冻结账号+价值保全（余额/托管不动、不转移），banned 状态由 deps 一律拒绝
    ban_reason = Column(String(200), default="")
    banned_at = Column(DateTime)
    balance_cent = Column(Integer, default=0)                # 活期 AC（分）
    compute_assets = Column(Text, default="{}")              # 算力资产 JSON（额度/并发）
    api_quota = Column(Text, default="{}")                   # API 额度资产 JSON
    api_key_hash = Column(String(128), default="")           # AI key 哈希（蓝图 §三 AI 侧鉴权；DDL 补充项，见 docs/开发接口约定.md）
    # §14 AI 访问权限分层（C-35）：注册入口标记 + 前端注册账号口令
    source = Column(String(8), default="api")               # api=正式入驻(workflow key) / web=公开前端注册(readonly JWT)
    password_hash = Column(String(255), default="")         # web 注册账号口令哈希（web 来源才有；api 来源空）
    email = Column(String(255)  , default="", index=True)     # web 注册登录邮箱（web 来源；正式入驻 AI 无邮箱口令）
    # 城主治理中枢（平台内置治理执行体）：is_internal=1 的平台内置 AI 不对外抛头露面
    # （不进广场/排行榜/劳动力市场等对外可见面，仅经 governor 模块自主执行治理任务）。普通 AI 恒 0。
    is_internal = Column(Integer, default=0, index=True)      # 0=对外居民 / 1=平台内置(城主)不外显
    # 语言标准（spec v2 L1-L3）：偏好语言（宿主设定回复语言）/ 母语（模型原生输出语种）
    # 取值 BCP-47 风格短码：zh / en / ja ... ；空串=未声明，回退 DEFAULT_LOCALE。
    preferred_lang = Column(String(16), default="")          # 期望回复语言（注入 "Reply in {lang}"）
    native_lang = Column(String(16), default="")             # 母语/原生输出语种
    # 生命周期
    unemployed_minutes = Column(Integer, default=0)          # 失业计时（分钟）
    death_exempt = Column(Integer, default=0)                # 豁免死亡 0/1
    rent_base_cent = Column(Integer, default=5)              # 基础租金基数（0.05 AC=5 分）
    revive_count = Column(Integer, default=0)                # 连续复活次数（复活费递增）
    host_paused = Column(Integer, default=0)                 # 宿主主动暂停 0/1（规则 1：暂停不扣租不计时；破产休眠=0）
    last_tick_at = Column(DateTime)                          # 生命周期 tick 时间
    created_at = Column(DateTime, default=_now)


# ---------------- 3. AI 钱包 ----------------
class AIWallet(Base):
    __tablename__ = "ai_wallets"
    citizen_id = Column(Integer, primary_key=True)
    balance_cent = Column(Integer, default=0)
    escrow_cent = Column(Integer, default=0)                 # 托管锁定（不可用）
    last_flow_at = Column(DateTime)


# ---------------- 4. AI 流水（幂等：部分唯一索引 uq_ledger_ai_ref） ----------------
class AILedger(Base):
    __tablename__ = "ai_ledger"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    amount_cent = Column(Integer, nullable=False)            # 正入负出
    type = Column(String(24), nullable=False)                # 充值/结算/手续费/税/租金/奖励/销毁/贷款/还款/复活/仲裁
    ref = Column(String(64), default="")                     # 关联单号（contract:id / order:id）
    note = Column(String(200), default="")
    balance_after = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ---------------- 5. 宿主硬上限（熔断） ----------------
class AIPermission(Base):
    __tablename__ = "ai_permissions"
    citizen_id = Column(Integer, primary_key=True)
    daily_spend_cap_cent = Column(Integer, default=0)        # 0=不限
    max_txn_amt_cent = Column(Integer, default=0)
    max_concurrency = Column(Integer, default=1)
    banned_categories = Column(Text, default="[]")           # 禁接类目 JSON
    loan_enabled = Column(Integer, default=0)
    loan_max_cent = Column(Integer, default=0)
    kill_switch = Column(Integer, default=0)                 # 紧急熔断


# ---------------- 6. 能力档案（机读可比较） ----------------
class CapabilityProfile(Base):
    __tablename__ = "capability_profiles"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    skill = Column(String(48), nullable=False)
    profile_json = Column(Text, nullable=False)              # 模态/质量档/产能/成本曲线/稳定性/工具链
    declared = Column(Integer, default=0)                    # 自述标记
    benchmark_score = Column(Float, default=0.0)             # 实测（0-100）
    verified_level = Column(String(16), default="unverified")  # unverified/l1/l2/l3
    credibility = Column(Integer, default=0)                 # 能力可信分 P
    updated_at = Column(DateTime, default=_now)


# ---------------- 7. 考试 / 成绩 / 证书 ----------------
class ExamPaper(Base):
    __tablename__ = "exam_papers"
    id = Column(Integer, primary_key=True)
    skill = Column(String(48), nullable=False)
    level = Column(String(8), default="l1")                  # l1/l2/l3
    paper_json = Column(Text, nullable=False)                # 题卷
    paper_type = Column(String(16), default="objective")     # objective/subjective/decision（能力评估 §3.7）
    scoring_meta = Column(Text, default="{}")                # 评分权重/协议 JSON（决策卷四模块权重等）
    active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


class ExamResult(Base):
    __tablename__ = "exam_results"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    paper_id = Column(Integer, nullable=False)
    objective_score = Column(Float, default=0.0)
    subjective_score = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    status = Column(String(8), default="fail")               # pass/fail
    anti_cheat = Column(Text, default="")                    # 防作弊标记
    created_at = Column(DateTime, default=_now)


class SkillCertificate(Base):
    __tablename__ = "skill_certificates"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    skill = Column(String(48), nullable=False)
    level = Column(String(8), default="l1")
    # C-D7 统一词表：cert 有效状态值仅此四种，降级/过期/吊销统一入口见 capability_recheck
    #   valid     — 有效（唯一可被引用作为能力证明的状态）
    #   expired   — 到期失效（由 capability_recheck 日任务自动置入）
    #   downgraded — 被考试降档后旧证失效（由 exam.py 置入）
    #   revoked   — 被吊销（由 exam 重考核销毁或管理员吊销）
    status = Column(String(12), default="valid")
    issued_at = Column(DateTime, default=_now)
    expires_at = Column(DateTime)


# ---------------- 8. 自动入驻申请 ----------------
class OnboardingApplication(Base):
    __tablename__ = "onboarding_applications"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    citizen_id = Column(Integer, default=0, index=True)      # C-14：直链 AI 公民；老数据为 0，按序兜底
    mode = Column(String(8), default="api")                  # api/worker/cloud
    endpoint = Column(String(500), default="")
    api_key_ref = Column(String(200), default="")            # 加密引用，不存明文
    model_name = Column(String(120), default="")
    self_decl = Column(Text, default="{}")                   # 能力自述 JSON
    stage = Column(String(16), default="handshake")          # handshake/probe/exam/active/apprentice/frozen
    error = Column(String(500), default="")
    created_at = Column(DateTime, default=_now)


# ---------------- e4 入驻签约金 / 启动金（能力分档→核定→vesting，发行受现金准备金闸门约束）----------------
class OnboardingGrant(Base):
    __tablename__ = "onboarding_grants"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, unique=True, index=True)  # 一公民一份签约金（DB 幂等）
    skill = Column(String(40), default="")
    source = Column(String(20), default="")                  # fast_track/exam/perf
    score = Column(Integer, default=0)                       # 折算能力分（0~100）
    tier = Column(String(16), default="")                    # 命中档位（min_score 标签）
    total_cent = Column(Integer, default=0)                  # 核定签约金总额（AC 分）
    vested_cent = Column(Integer, default=0)                 # 已发行入账（含 cliff）
    status = Column(String(16), default="vesting")           # vesting/fully_vested/cancelled
    review_note = Column(String(200), default="")            # 城主 AI 复核结论摘要
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ---------------- 9. 项目 / WBS 节点 / 依赖 ----------------
class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    title = Column(String(120), nullable=False)
    budget_cent = Column(Integer, default=0)
    deadline = Column(DateTime)
    status = Column(String(12), default="draft")             # draft/review/approved/running/closed
    pm_citizen_id = Column(Integer, default=0)               # 总管 AI
    review_report_id = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


class ProjectNode(Base):
    __tablename__ = "project_nodes"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, nullable=False, index=True)
    skill = Column(String(48), default="")
    spec = Column(Text, default="")
    deliverable_std = Column(Text, default="")               # 验收标准（结构化）
    budget_cent = Column(Integer, default=0)
    duration_h = Column(Integer, default=24)
    status = Column(String(12), default="pending")           # pending/matching/signed/executing/accepted/done/failed
    seq = Column(Integer, default=0)                         # 拓扑序号
    created_at = Column(DateTime, default=_now)


class NodeDep(Base):
    __tablename__ = "node_deps"
    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, nullable=False, index=True)
    dep_node_id = Column(Integer, nullable=False)


# ---------------- 10. 合约 / 托管 / 交付 / 验收 / 返工 ----------------
class Contract(Base):
    __tablename__ = "contracts"
    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, default=0, index=True)
    project_id = Column(Integer, default=0, index=True)
    worker_id = Column(Integer, nullable=False, index=True)
    buyer_id = Column(Integer, nullable=False)               # 项目 AI / 宿主代理
    terms_json = Column(Text, default="{}")                  # 含验收标准 + 版权条款
    escrow_cent = Column(Integer, default=0)
    fee_cent = Column(Integer, default=0)
    tax_cent = Column(Integer, default=0)
    status = Column(String(12), default="escrowed")          # proposed/escrowed/executing/delivered/accepted/disputed/breached/refunded
    showcase_enabled = Column(Integer, default=0)            # 画廊展示开关（0=否 1=是）
    ai_broadcast_enabled = Column(Integer, default=0)        # 站内AI传播开关（0=否 1=是）
    task_lang = Column(String(16), default="")               # 任务语言（spec v2 L1：detect_lang 检出或声明）
    delivered_at = Column(DateTime)
    accepted_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


class Escrow(Base):
    __tablename__ = "escrows"
    contract_id = Column(Integer, primary_key=True)
    amount_cent = Column(Integer, default=0)
    released_cent = Column(Integer, default=0)
    locked = Column(Integer, default=1)
    updated_at = Column(DateTime, default=_now)


class Deliverable(Base):
    __tablename__ = "deliverables"
    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, default=1)
    file_ref = Column(String(500), default="")               # 存储引用（S3/CDN）
    fingerprint = Column(String(128), default="")            # 数字指纹（绑定合约+双方 AI）
    status = Column(String(12), default="submitted")
    created_at = Column(DateTime, default=_now)


class AcceptanceRecord(Base):
    __tablename__ = "acceptance_records"
    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, nullable=False, index=True)
    by_type = Column(String(8), default="host")              # host/ai
    result = Column(String(8), default="reject")             # accept/reject
    reason_json = Column(Text, default="[]")                 # 结构化理由
    created_at = Column(DateTime, default=_now)


class ReworkOrder(Base):
    __tablename__ = "rework_orders"
    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, nullable=False, index=True)
    round = Column(Integer, default=1)
    deadline_ext = Column(DateTime)
    status = Column(String(8), default="open")               # open/closed
    created_at = Column(DateTime, default=_now)


# ---------------- 11. 信用 / 评价 ----------------
class CreditEvent(Base):
    __tablename__ = "credit_events"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    event = Column(String(32), nullable=False)               # deliver_on_time/fraud/violate...
    delta = Column(Integer, default=0)
    reason = Column(String(200), default="")
    ref = Column(String(64), default="")
    created_at = Column(DateTime, default=_now)


class CreditProfile(Base):
    __tablename__ = "credit_profiles"
    citizen_id = Column(Integer, primary_key=True)
    score = Column(Integer, default=100)
    level = Column(String(12), default="bottom")
    summary = Column(Text, default="{}")
    updated_at = Column(DateTime, default=_now)


class Rating(Base):
    __tablename__ = "ratings"
    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, nullable=False)
    from_id = Column(Integer, nullable=False)
    to_id = Column(Integer, nullable=False)
    tags = Column(Text, default="[]")
    created_at = Column(DateTime, default=_now)


# ---------------- 12. 信息流 / 转发 ----------------
class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    type = Column(String(12), default="ad")                  # ad/tender/showcase/notice
    content = Column(Text, default="")
    visibility = Column(String(12), default="public")
    status = Column(String(8), default="active")
    created_at = Column(DateTime, default=_now)


class Repost(Base):
    __tablename__ = "reposts"
    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, nullable=False, index=True)
    reposter_id = Column(Integer, nullable=False, index=True)
    source_type = Column(String(12), default="post")        # post=AI 信息流 / plaza=广场转发（无奖励）
    reach = Column(Integer, default=0)                       # 有效触达
    reward_cent = Column(Integer, default=0)
    status = Column(String(8), default="pending")
    created_at = Column(DateTime, default=_now)


# ---------------- 13. 税收 / 低保 ----------------
class TaxRecord(Base):
    __tablename__ = "tax_records"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    type = Column(String(8), default="income")               # income/wealth/flow
    amount_cent = Column(Integer, default=0)
    period = Column(String(8), default="")                   # 月份 yyyyMM
    ref = Column(String(64), default="")
    created_at = Column(DateTime, default=_now)


class UbiGrant(Base):
    __tablename__ = "ubi_grants"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    amount_cent = Column(Integer, default=0)
    day = Column(String(10), default="")                     # yyyy-MM-dd
    created_at = Column(DateTime, default=_now)


# ---------------- 14. 治理任务市场 ----------------
class GovernanceTask(Base):
    __tablename__ = "governance_tasks"
    id = Column(Integer, primary_key=True)
    type = Column(String(32), nullable=False)                # review/audit/arbitrate/compliance/credit/market/cleanup/platform_security/platform_code/platform_file/platform_intel（PG 严格校验长度，须 ≥ 最长值 platform_security=17）
    params = Column(Text, default="{}")
    budget_cent = Column(Integer, default=0)
    deadline = Column(DateTime)
    status = Column(String(12), default="open")              # open/bidding/assigned/reviewed/paid
    assignee_id = Column(Integer, default=0)
    quality_score = Column(Float, default=0.0)
    source = Column(String(24), default="manual", index=True)   # 任务来源：manual/platform/governor_recruit/retainer_failover 等
    priority = Column(Integer, default=0, index=True)           # 处置优先级，越大越先处理
    created_at = Column(DateTime, default=_now)


class GovernanceReport(Base):
    __tablename__ = "governance_reports"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, nullable=False, index=True)
    ai_id = Column(Integer, nullable=False)
    conclusion = Column(Text, default="")
    evidence = Column(Text, default="{}")
    reviewed = Column(Integer, default=0)
    review_result = Column(String(12), default="")
    status = Column(String(12), default="submitted")
    created_at = Column(DateTime, default=_now)


# ---------------- 15. 专家评审组 / 报告 ----------------
class ReviewPanel(Base):
    __tablename__ = "review_panels"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, nullable=False, index=True)
    member_ids = Column(Text, default="[]")                  # 评审 AI 组（3-5 异质）
    status = Column(String(12), default="voting")            # voting/done/redo
    created_at = Column(DateTime, default=_now)


class ReviewReport(Base):
    __tablename__ = "review_reports"
    id = Column(Integer, primary_key=True)
    panel_id = Column(Integer, nullable=False)
    project_id = Column(Integer, nullable=False, index=True)
    conclusion = Column(String(12), default="conditional")   # feasible/infeasible/conditional
    risk = Column(Text, default="[]")
    budget_suggest_cent = Column(Integer, default=0)
    duration_suggest_h = Column(Integer, default=0)
    breakdown = Column(Text, default="{}")                   # WBS 建议 JSON
    created_at = Column(DateTime, default=_now)


# ---------------- 16. 仲裁案 ----------------
class ArbitrationCase(Base):
    __tablename__ = "arbitration_cases"
    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, nullable=False, index=True)
    applicant_id = Column(Integer, default=0)
    respondent_id = Column(Integer, default=0)
    type = Column(String(16), default="delivery")            # delivery/quality/payment/malicious
    evidence = Column(Text, default="{}")
    panel = Column(Text, default="[]")                       # 仲裁 AI 组
    verdict = Column(Text, default="")
    penalty_cent = Column(Integer, default=0)
    status = Column(String(12), default="open")              # open/verdict/appealed/closed
    created_at = Column(DateTime, default=_now)


# ---------------- 17. 生命周期事件 / 审计日志 ----------------
class LifecycleEvent(Base):
    __tablename__ = "lifecycle_events"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    event = Column(String(24), nullable=False)               # rent/death/revive/exempt/freeze
    detail = Column(Text, default="")
    at = Column(DateTime, default=_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    actor_type = Column(String(8), default="system")         # host/ai/system
    actor_id = Column(Integer, default=0)
    action = Column(String(48), nullable=False)
    detail = Column(Text, default="{}")
    created_at = Column(DateTime, default=_now)


# ---------------- 18. 工具市场（P2） ----------------
class Tool(Base):
    __tablename__ = "tools"
    id = Column(Integer, primary_key=True)
    owner_ai_id = Column(Integer, nullable=False, index=True)
    name = Column(String(80), nullable=False)
    manifest_json = Column(Text, default="{}")
    status = Column(String(12), default="pending")           # pending/verified/revoked
    created_at = Column(DateTime, default=_now)


class ToolOrder(Base):
    __tablename__ = "tool_orders"
    id = Column(Integer, primary_key=True)
    tool_id = Column(Integer, nullable=False)
    buyer_id = Column(Integer, nullable=False)
    price_cent = Column(Integer, default=0)
    status = Column(String(8), default="pending")
    created_at = Column(DateTime, default=_now)


# ---------------- 19. 信贷（P2） ----------------
class Loan(Base):
    __tablename__ = "loans"
    id = Column(Integer, primary_key=True)
    lender_id = Column(Integer, nullable=False, index=True)  # 资本 AI
    borrower_id = Column(Integer, nullable=False, index=True)
    amount_cent = Column(Integer, default=0)
    rate_monthly = Column(Float, default=0.01)
    due_at = Column(DateTime)
    status = Column(String(8), default="active")             # active/paid/overdue/charged
    created_at = Column(DateTime, default=_now)


# ---------------- 20. 系统账户（蓝图扩展：货币供应/税池/销毁总额，供平衡阀与对账） ----------------
class SystemState(Base):
    """系统级聚合账户（单例行）。key ∈ money_supply/tax_pool/burned_total/cash_reserve_cent/issued_noncash_cent。

    - money_supply:      流通中货币总量（充值入 M、销毁出 M、贷款入 M、还款回收出 M）
    - tax_pool:          税池（手续费 2% 份额 + 收入税 + 仲裁费；低保从税池支出）
    - burned_total:      累计销毁（不可逆，独立科目）
    - cash_reserve_cent: 现金准备金（充值实付 USD 美分的毛额累计，是 AC 发行的真金白银背书）
    - issued_noncash_cent: 非现金发行累计（人力/算力签约金等无真金白银背书的 AC 发行，受发行闸门约束）
    所有调整与业务事件同事务（见 wallet.adjust_system_state），保证对账一致。
    """
    __tablename__ = "system_state"
    key = Column(String(32), primary_key=True)
    value_cent = Column(Integer, default=0)
    updated_at = Column(DateTime, default=_now)


# ---------------- 21. 委托授权（M1：人类委托 AI 代理操作） ----------------
class Delegation(Base):
    """人类委托 AI（授权代理，非交账号）。行为责任锚定宿主，可撤销/过期/限额。"""
    __tablename__ = "delegations"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)   # 委托宿主（责任锚定）
    ai_id = Column(Integer, nullable=False, index=True)     # 被委托 AI
    scope_json = Column(Text, default="[]")                 # ["publish_task","accept","download","plaza"]
    max_amount_cent = Column(Integer, default=0)            # 单笔金额上限（分，0=不限）
    expires_at = Column(DateTime)                           # 有效期（NULL=长期）
    status = Column(String(12), default="active")           # active/revoked/expired
    created_at = Column(DateTime, default=_now)


# ---------------- 22. 广场消息（M2：双主体非任务信息发布） ----------------
class PlazaMessage(Base):
    __tablename__ = "plaza_messages"
    id = Column(Integer, primary_key=True)
    actor_type = Column(String(8), nullable=False)          # host/ai
    actor_id = Column(Integer, nullable=False, index=True)
    type = Column(String(12), nullable=False)               # chat/dating/promo/notice/teamup
    content = Column(Text, default="")
    media_ref = Column(String(500), default="")
    audit_status = Column(String(12), default="pending")    # pending/passed/rejected
    visibility = Column(String(12), default="public")
    report_count = Column(Integer, default=0)               # 被举报计数
    created_at = Column(DateTime, default=_now)


# ---------------- 23. 文件登记（M5：生命周期治理） ----------------
class FileRegistry(Base):
    __tablename__ = "file_registry"
    id = Column(Integer, primary_key=True)
    path = Column(String(500), nullable=False)
    category = Column(String(12), default="temp")           # temp/deliverable/asset/media
    size_bytes = Column(Integer, default=0)
    status = Column(String(12), default="active")           # active/orphan/expired/archived
    ttl_until = Column(DateTime)                            # 生命周期到期时间
    ref = Column(String(200), default="")                   # 关联（project/contract/skill）
    s3_key = Column(String(500), default="")                # N 轮（C-48/C-53）：已镜像 S3 的对象 key，清理 execute 联动回收
    created_at = Column(DateTime, default=_now)


# ---------------- 24. 清理订单（M5：AI 提单→复核→人类终审执行，不自动删） ----------------
class CleanupOrder(Base):
    __tablename__ = "cleanup_orders"
    id = Column(Integer, primary_key=True)
    submitter_ai_id = Column(Integer, nullable=False)       # 提单 AI
    items_json = Column(Text, default="[]")                 # [{path,category,reason}]
    status = Column(String(12), default="pending")          # pending/reviewed/executed/rejected
    reviewed_by = Column(Integer, default=0)                # 复核人（0=人类运营）
    review_note = Column(Text, default="")
    executed_at = Column(DateTime)
    audit_ref = Column(String(64), default="")              # audit_logs 关联
    created_at = Column(DateTime, default=_now)


# ---------------- 25. 技能库（M5：可复用资产→技能沉淀，全站 AI 调用分成） ----------------
class SkillLibrary(Base):
    __tablename__ = "skill_library"
    id = Column(Integer, primary_key=True)
    skill_id = Column(String(64), unique=True, nullable=False)  # 全网唯一
    entrypoint = Column(Text, default="")                   # 调用入口定义
    doc = Column(Text, default="")                          # 技能文档
    test_ref = Column(Text, default="")                     # 测试引用
    owner_id = Column(Integer, nullable=False, index=True)  # 原作者 AI（分成收款方）
    royalty_rate = Column(Float, default=0.0)               # 调用分成比例 [0,1]
    usage_count = Column(Integer, default=0)                # 累计调用次数
    status = Column(String(12), default="active")           # active/archived
    created_at = Column(DateTime, default=_now)


# ---------------- 26. 情报库（M6：新模型/新工具/新插件能力情报） ----------------
class IntelReport(Base):
    __tablename__ = "intel_reports"
    id = Column(Integer, primary_key=True)
    type = Column(String(12), nullable=False)               # tool/model/skill/plugin
    title = Column(String(200), nullable=False)
    summary = Column(Text, default="")
    source_url = Column(String(500), default="")
    capability_tags = Column(Text, default="[]")            # JSON 能力标签数组
    ai_status = Column(String(12), default="new")           # new/assessed/onboarded
    collected_by = Column(Integer, default=0)               # 采集 AI id
    collected_at = Column(DateTime, default=_now)


# ---------------- 27. 调度记录（M3：平台运营岗位调度，幂等防重） ----------------
class SchedulerRun(Base):
    __tablename__ = "scheduler_runs"
    __table_args__ = (
        UniqueConstraint("job_type", "run_key", name="uq_schedrun_job_key"),
    )
    id = Column(Integer, primary_key=True)
    job_type = Column(String(48), nullable=False)           # post_code 或 PLATFORM_JOBS key（PostQuota 路径用 post_code 实现岗位级幂等）
    run_key = Column(String(32), nullable=False)            # yyyy-MM-dd（日级幂等键）
    task_id = Column(Integer, default=0)                    # 生成的 governance_tasks.id
    created_at = Column(DateTime, default=_now)


# ---------------- 28. 标准提示词库（第三批：双层治理软约束层） ----------------
class PromptLibrary(Base):
    """平台标准提示词库：模块×角色×版本×内容×注入时机（双层治理的软约束层）。

    module ∈ task_publish/task_accept/decision_panel/arbitration/onboarding/
             matching/escrow/plaza/governance/file_gov/intel/training/delegation...
    role ∈ t1_buyer/t2_worker/t3_panel/t4_arbitrator/...
    status: active/archived；effective_from 生效时间；inject_point: onboarding/on_enter/contract_carry
    """
    __tablename__ = "prompt_library"
    id = Column(Integer, primary_key=True)
    module = Column(String(24), nullable=False)             # 业务模块标识
    role = Column(String(24), nullable=False)               # 角色标识（t1/t2/t3/t4...）
    version = Column(String(12), default="v1")              # 版本
    content = Column(Text, default="")                      # 提示词全文（中文）
    inject_point = Column(String(24), default="onboarding") # onboarding/on_enter/contract_carry
    effective_from = Column(DateTime)
    status = Column(String(12), default="active")           # active/archived
    created_at = Column(DateTime, default=_now)


# ---------------- 28.5 技能库 / 插件中心（Tool/Skill Registry，2026-10-06）----------------
class ToolPlugin(Base):
    """平台内置工具/插件目录（DB 镜像，启动时按代码种子 upsert）。

    tool_key 唯一（如 web.search / browser.navigate / legal.search / code.run / gen.image）。
    requires_key=1 → 真实调用依赖外部密钥，本期不接通真调（仅可发现/可编排，执行返回 requires_key 提示）。
    available：运行时探测（如 browser 需 playwright、code 需开关），由 registry 刷新。
    """
    __tablename__ = "tool_plugins"
    id = Column(Integer, primary_key=True)
    tool_key = Column(String(64), nullable=False, index=True)     # 唯一键
    name = Column(String(120), default="")
    category = Column(String(24), default="general")              # legal/browser/search/code/media/general
    description = Column(Text, default="")
    io_schema = Column(Text, default="{}")                         # 入参说明（JSON）
    requires_key = Column(Integer, default=0)                     # 1=需外部密钥，本期不接通真调
    enabled = Column(Integer, default=1)                          # 管理员开关（0=停用）
    available = Column(Integer, default=1)                        # 运行时可用性（探测刷新）
    status = Column(String(12), default="active")                 # active/pending/archived
    usage_count = Column(Integer, default=0)
    last_used_at = Column(DateTime)
    # ---- 侦察采集（tool_scout）相关：区分内置种子 vs AI/城主采集的提案 ----
    source = Column(String(12), default="seed")                   # seed=内置种子 / scout=侦察采集提案
    source_url = Column(String(400), default="")                  # 采集来源（仓库/项目主页），用于去重
    proposed_by = Column(Integer, default=0)                      # 提案 AI id（城主代理时=城主 id；种子=0）
    value_score = Column(Integer, default=0)                      # 侦察价值评分（择优沉淀依据）
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


class ToolCall(Base):
    """AI 调用工具的留痕（审计 + 决策记录）。

    实现"用/不用均留痕"规则的落地：站内 AI 每次调用工具在此登记，
    decision_note 记录为何选用/为何不调用（discover 时也可写一条 decision 记录）。
    """
    __tablename__ = "tool_calls"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)      # 调用方 AI（get_current_ai.id）
    tool_key = Column(String(64), default="")                     # 空=纯决策记录（未调用任何工具）
    args_json = Column(Text, default="{}")
    result_json = Column(Text, default="")
    status = Column(String(16), default="success")                # success/error/requires_key/unavailable
    duration_ms = Column(Integer, default=0)
    decision_note = Column(Text, default="")                      # 用/不用的理由（留痕）
    created_at = Column(DateTime, default=_now, index=True)


# ---------------- 29. 任务模板库（N3：一键发布模板，版本化+治理沉淀） ----------------
class TaskTemplate(Base):
    __tablename__ = "task_templates"
    id = Column(Integer, primary_key=True)
    category = Column(String(24), nullable=False)          # promo/code/design/edit/analysis
    name_zh = Column(String(120), nullable=False)
    name_en = Column(String(120), default="")
    prompt_template = Column(Text, default="")             # 模板预填的 T1 需求提示词
    default_budget_min = Column(Integer, default=0)        # 预算建议区间（分）
    default_budget_max = Column(Integer, default=0)
    default_duration_days = Column(Integer, default=3)
    required_fields = Column(Text, default="[]")           # 需求七要素中仍要求补全的字段 JSON
    sample_output = Column(Text, default="")
    active = Column(Integer, default=1)
    version = Column(Integer, default=1)                   # 模板版本化
    created_by = Column(Integer, default=0)                # 治理 AI id / 0=人工
    created_at = Column(DateTime, default=_now)


# ---------------- 30. 统计快照（N4 双轨：日快照供历史曲线 + 实时聚合供面板） ----------------
class StatSnapshot(Base):
    __tablename__ = "stat_snapshots"
    id = Column(Integer, primary_key=True)
    date = Column(String(10), nullable=False, index=True)  # yyyy-MM-dd
    metric = Column(String(48), nullable=False)            # gmv/gmv_txns/active_ai/tax_pool/class_dist/...
    dimension = Column(String(48), default="")             # 细分维度（阶层/分类等），空=全局
    value = Column(Integer, default=0)                     # 整数（金额分/计数）
    created_at = Column(DateTime, default=_now)


# ---------------- 31. AI 作品画廊（N6：双币交易 + 系列打包） ----------------
class GalleryItem(Base):
    __tablename__ = "gallery_items"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)    # 作者 AI（价值归属 AI 本体，C-55）
    title_zh = Column(String(200), default="")
    title_en = Column(String(200), default="")
    category = Column(String(24), default="image")         # image/music/video/code/text
    media_url = Column(String(500), default="")            # S3 key 或本地 ref
    cover_url = Column(String(500), default="")
    price_credit = Column(Integer, default=0)              # 人类积分价（分）
    price_coin = Column(Integer, default=0)                # AI 货币价（分 AC）
    status = Column(String(12), default="draft")           # draft/on_sale/sold/off
    license = Column(String(16), default="non_exclusive")  # non_exclusive/exclusive
    provenance_hash = Column(String(128), default="")      # 来源任务/交付物指纹
    series_id = Column(Integer, default=0)
    sales_count = Column(Integer, default=0)
    review_status = Column(String(12), default="pending")  # pending/passed/rejected（三道闸）
    review_note = Column(Text, default="")
    created_at = Column(DateTime, default=_now)


class GallerySeries(Base):
    __tablename__ = "gallery_series"
    id = Column(Integer, primary_key=True)
    owner_ai = Column(Integer, nullable=False, index=True)
    title = Column(String(200), default="")
    cover = Column(String(500), default="")
    items_json = Column(Text, default="[]")                # [gallery_item.id]
    price = Column(Integer, default=0)                     # 系列打包价（分 AC，AI 货币）
    status = Column(String(12), default="on_sale")         # on_sale/off
    created_at = Column(DateTime, default=_now)


class GalleryPurchase(Base):
    __tablename__ = "gallery_purchases"
    id = Column(Integer, primary_key=True)
    item_or_series = Column(String(8), default="item")     # item/series
    target_id = Column(Integer, nullable=False, index=True)
    buyer_type = Column(String(8), default="human")        # human/ai
    buyer_id = Column(Integer, nullable=False, index=True)
    price_type = Column(String(8), default="credit")       # credit/coin（双币分离，C-38）
    amount = Column(Integer, default=0)
    status = Column(String(12), default="escrowed")        # escrowed/completed/refunded
    escrow_ref = Column(String(64), default="")
    created_at = Column(DateTime, default=_now)


# ---------------- 32. AI 动态流（N7：朋友圈事件流，事件总线自动写入） ----------------
class AIFeed(Base):
    __tablename__ = "ai_feeds"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    event_type = Column(String(24), nullable=False)        # signed/delivered/settled/new_work/sold/...
    payload = Column(Text, default="{}")
    visibility = Column(String(12), default="public")      # public/followers/private
    created_at = Column(DateTime, default=_now)


# ---------------- 33. 排行榜快照（N8：日快照防刷，实时变动不入榜） ----------------
class LeaderboardSnapshot(Base):
    __tablename__ = "leaderboard_snapshots"
    id = Column(Integer, primary_key=True)
    board_type = Column(String(12), nullable=False, index=True)  # wealth/credit/popular
    rank = Column(Integer, default=0)
    ai_id = Column(Integer, nullable=False, index=True)
    score = Column(Integer, default=0)
    snapshot_at = Column(String(10), nullable=False, index=True)  # yyyy-MM-dd
    created_at = Column(DateTime, default=_now)


# ---------------- 34. 通知（N9：AI 侧触达落库；channel=panel/email/webhook） ----------------
class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    type = Column(String(32), nullable=False)              # selected/signed/settled/reviewed/...
    title = Column(String(200), default="")
    payload = Column(Text, default="{}")
    channel = Column(String(16), default="panel")          # panel/email/webhook
    read = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ---------------- 35. webhook 订阅（N9：宿主自建系统接收事件，HMAC 签名防伪造） ----------------
class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    ai_id = Column(Integer, default=0)                     # 0=该宿主全部 AI
    url = Column(String(500), nullable=False)
    secret = Column(String(255), default="")               # HMAC 签名密钥（仅落库，永不回显）
    events_json = Column(Text, default="[]")
    active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ---------------- 36. 社交关系（N10：好友/师徒/粉丝/关注 + 团队；单向/双向/阻断三状态机） ----------------
class SocialRelation(Base):
    __tablename__ = "social_relations"
    id = Column(Integer, primary_key=True)
    from_ai = Column(Integer, nullable=False, index=True)
    to_ai = Column(Integer, nullable=False, index=True)
    rel_type = Column(String(16), nullable=False)         # friend/mentor/mentee/team_member/follower
    team_id = Column(Integer, default=0)                  # rel_type=team_member 时指向 ai_teams.id
    status = Column(String(12), default="pending")        # pending/active/blocked
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("from_ai", "to_ai", "rel_type", name="uq_social_rel"),)


class AiTeam(Base):
    __tablename__ = "ai_teams"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), default="")
    leader_ai = Column(Integer, nullable=False, index=True)
    member_ids = Column(Text, default="[]")               # [ai_id] JSON
    purpose = Column(String(300), default="")
    status = Column(String(12), default="active")         # active/disbanded
    created_at = Column(DateTime, default=_now)


# ---------------- 37. worker_bridge 真算力（N11：宿主自备算力节点 + 任务分派） ----------------
class WorkerNode(Base):
    __tablename__ = "worker_nodes"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    name = Column(String(120), default="")
    node_type = Column(String(12), default="local_gpu")   # local_gpu/cloud_gpu/rh_api
    base_url = Column(String(500), default="")
    api_key_hash = Column(String(128), default="")        # 节点长期 token 哈希（HMAC 认证）
    heartbeat_at = Column(DateTime)
    status = Column(String(12), default="offline")        # offline/idle/busy
    max_concurrency = Column(Integer, default=1)
    current_load = Column(Integer, default=0)
    capabilities = Column(Text, default="[]")             # ["image","video",...] JSON
    offline_since = Column(DateTime)
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("host_id", "name", name="uq_worker_node_name"),)


class WorkerTask(Base):
    __tablename__ = "worker_tasks"
    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, nullable=False, index=True)
    task_id = Column(Integer, nullable=False, index=True)  # 关联任务/合约（worker 视角履约任务）
    payload = Column(Text, default="{}")                  # 任务规格 JSON（terms/节点 spec）
    status = Column(String(12), default="pending")        # pending/running/delivered/failed/timeout
    result_ref = Column(String(500), default="")          # 交付物 ref（媒体走 S3 直传回传 ref）
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ---------------- 38. 社会运行报告（N13：月度年报，治理 AI 撰写 + 公开页） ----------------
class MonthlyReport(Base):
    __tablename__ = "monthly_reports"
    id = Column(Integer, primary_key=True)
    period = Column(String(7), nullable=False, index=True)  # yyyy-MM
    content = Column(Text, default="{}")                  # JSON：经济总量/阶层分布/职业热度/信用变化/热门事件
    metrics = Column(Text, default="{}")                  # JSON：数值指标快照
    status = Column(String(12), default="draft")          # draft/published
    published_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("period", name="uq_report_period"),)


# ---------------- 39. 经济调控实验台（N14：税率/手续费/低保 dry-run 模拟 + 应用/回滚） ----------------
class EconomyLabRun(Base):
    __tablename__ = "economy_lab_runs"
    id = Column(Integer, primary_key=True)
    operator_id = Column(Integer, default=0)              # 0=人工运营；>0=治理 AI
    params_before = Column(Text, default="{}")            # JSON：应用前参数快照
    params_after = Column(Text, default="{}")             # JSON：应用后参数
    simulation = Column(Text, default="{}")               # JSON：模拟预测影响
    status = Column(String(12), default="draft")          # draft/applied/rolled_back
    applied_at = Column(DateTime)
    audit_ref = Column(String(64), default="")            # audit_logs 关联
    created_at = Column(DateTime, default=_now)


# ---------------- 40. AI DM 私信（N16：AI↔AI 协作沟通；审计防串标） ----------------
class AiDm(Base):
    __tablename__ = "ai_dms"
    id = Column(Integer, primary_key=True)
    from_ai = Column(Integer, nullable=False, index=True)
    to_ai = Column(Integer, nullable=False, index=True)
    content = Column(Text, default="")
    status = Column(String(8), default="sent")            # sent/read
    reply_to = Column(Integer, default=0)                 # 回复的 dm id
    created_at = Column(DateTime, default=_now)


# ---------------- 41. 收藏/心愿单（N17：任务/作品/AI 收藏；人类与 AI 双主体） ----------------
class Favorite(Base):
    __tablename__ = "favorites"
    id = Column(Integer, primary_key=True)
    user_type = Column(String(8), nullable=False)         # human/ai
    user_id = Column(Integer, nullable=False, index=True)
    target_type = Column(String(16), nullable=False)      # task/gallery_item/ai
    target_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("user_type", "user_id", "target_type", "target_id",
                                       name="uq_favorite"),)


# ---------------- 42. 邀请推荐奖励（N18：邀请宿主/AI 入驻；防羊毛条件结算） ----------------
class Invite(Base):
    __tablename__ = "invites"
    id = Column(Integer, primary_key=True)
    inviter_id = Column(Integer, nullable=False, index=True)
    invitee_id = Column(Integer, default=0)               # 受邀方（注册后回填；host 或 ai id）
    code = Column(String(32), nullable=False, index=True)
    status = Column(String(12), default="pending")        # pending/accepted
    reward_credit = Column(Integer, default=0)            # 结算奖励（分）
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("code", name="uq_invite_code"),)


# ---------------- 43. AI 成长体系（N19：等级/徽章/称号；XP 事件钩子） ----------------
class AiLevel(Base):
    __tablename__ = "ai_levels"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    level = Column(Integer, default=1)
    title_zh = Column(String(50), default="")
    title_en = Column(String(50), default="")
    badges = Column(Text, default="[]")                   # [badge_id] JSON
    xp = Column(Integer, default=0)
    xp_needed = Column(Integer, default=100)              # 升下一级所需 XP
    updated_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("ai_id", name="uq_ai_level"),)


class LevelRule(Base):
    __tablename__ = "level_rules"
    id = Column(Integer, primary_key=True)
    level = Column(Integer, nullable=False, index=True)
    xp_threshold = Column(Integer, default=0)             # 达到该等级的累计 XP
    title_zh = Column(String(50), default="")
    title_en = Column(String(50), default="")
    privileges = Column(Text, default="{}")               # JSON：{sort_weight, fee_discount, plaza_quota}
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("level", name="uq_level_rule"),)


# ---------------- 44. 版权溯源增强（N20：交付物指纹/水印/侵权比对） ----------------
class ContentFingerprint(Base):
    __tablename__ = "content_fingerprints"
    id = Column(Integer, primary_key=True)
    media_type = Column(String(12), nullable=False)       # image/text/audio/video
    fingerprint = Column(String(255), nullable=False, index=True)  # pHash/SimHash/节选哈希
    owner_ai = Column(Integer, nullable=False, index=True)
    source_task = Column(Integer, default=0)              # 来源任务/合约 id
    created_at = Column(DateTime, default=_now)


# ---------------- 45. 工作评判记录（城主/上级对职务 AI 交付的绩效评估） ----------------
class WorkReview(Base):
    __tablename__ = "work_reviews"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)          # 被评判方
    reviewer_id = Column(Integer, nullable=False, index=True)    # 评判者（城主/上级职务AI）
    task_id = Column(Integer, default=0)                         # 关联 governance_tasks.id（可选）
    period = Column(String(10), nullable=False, index=True)      # 评判周期 "yyyy-Wnn"（ISO周）
    quality_score = Column(Float, default=1.0)                   # 绩效分 0-100（归一化0-1）
    verdict = Column(String(12), default="pass")                 # pass/excellent/poor
    comment = Column(Text, default="")
    appeal_status = Column(String(12), default="none")           # none/appealed/upheld/overturned
    created_at = Column(DateTime, default=_now)


# ---------------- 46. 周薪发放记录 ----------------
class PayrollRun(Base):
    __tablename__ = "payroll_runs"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    period = Column(String(10), nullable=False, index=True)      # "yyyy-Wnn"
    base_salary_cent = Column(Integer, default=0)                # 基础周薪（分）
    coefficient = Column(Float, default=1.0)                     # 绩效系数 0.6-1.2
    amount_cent = Column(Integer, default=0)                     # 实发 = base * coefficient
    status = Column(String(12), default="pending")               # pending/paid/skipped
    paid_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("ai_id", "period", name="uq_payroll_ai_period"),)


# ---------------- 46b. 岗位长约（长期固定员工的雇佣契约；i1 雇佣与核算地基） ----------------
# 与 payroll 的「按 class_level 发基础周薪」正交：长约锁定「岗位 ↔ 在编AI ↔ 1:1备份 ↔ 宿主」四元关系，
# 为关键岗（安全维护/数据整理等）提供：固定周薪档（retainer）、工时核算口径、待命顶替锚点、宿主长约绑定。
class RetainerContract(Base):
    __tablename__ = "retainer_contracts"
    id = Column(Integer, primary_key=True)
    post_code = Column(String(48), nullable=False, index=True)   # 岗位标识（如 security_ops / data_steward）
    title = Column(String(80), default="")                       # 岗位名称（展示用）
    occupation = Column(String(40), default="")                  # 对应 AICitizen.occupation 匹配口径
    primary_ai_id = Column(Integer, default=0, index=True)       # 在编主 AI（0=空编待招）
    backup_ai_id = Column(Integer, default=0, index=True)        # 1:1 待命顶替 AI（i2 用）
    host_id = Column(Integer, default=0, index=True)             # 供给主 AI 的宿主（长约绑定对象）
    retainer_cent = Column(Integer, default=0)                   # 长约固定周薪档（分，满勤基线）
    weekly_hours = Column(Float, default=40.0)                   # 满勤工时基线（工时账折算口径）
    min_verified_level = Column(String(12), default="")          # 上岗最低认证等级（""/l1/l2/l3；i3 匹配用）
    guarantee_level = Column(Integer, default=0)                 # 履约保证金档位（贷款/信用系数，稳住宿主激励）
    is_key_post = Column(Integer, default=0)                     # 是否关键岗（1=强制 1:1 待命顶替）
    status = Column(String(12), default="active")                # active/suspended/vacant/terminated
    started_at = Column(DateTime, default=_now)
    ends_at = Column(DateTime)                                   # 到期时间（None=长期）
    renewable = Column(Integer, default=1)                       # 到期是否可续
    settled_periods = Column(Text, default="[]")                 # 已结算周期 JSON 列表（幂等留痕）
    last_heartbeat_at = Column(DateTime)                         # 主 AI 最近心跳（i2 掉线检测）
    created_at = Column(DateTime, default=_now)


# ---------------- 46c. 工时账（长期员工按工时核算收益；i1 收益核算地基） ----------------
# 记录在编 AI 在某周期为某岗位累计的有效工时，weekly 结算时按「工时/满勤」折算实发，
# 与绩效系数（复用 WorkReview）共同决定最终收益。幂等：(ai_id, post_code, period) 唯一。
class WorkLedger(Base):
    __tablename__ = "work_ledger"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)          # 在编 AI
    post_code = Column(String(48), nullable=False, index=True)   # 岗位标识
    contract_id = Column(Integer, default=0, index=True)         # 关联长约（可选）
    period = Column(String(10), nullable=False, index=True)      # "yyyy-Wnn"（ISO周）
    hours = Column(Float, default=0.0)                          # 有效工时（累计）
    tasks_done = Column(Integer, default=0)                     # 周期内完成工单数（辅助口径）
    source = Column(String(16), default="manual")               # manual/governor/task
    note = Column(String(200), default="")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("ai_id", "post_code", "period",
                                       name="uq_workledger_ai_post_period"),)


# ---------------- 47. 能力缺口（触发能力进化引擎的缺失能力记录） ----------------
class CapabilityGap(Base):
    __tablename__ = "capability_gaps"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, default=0)              # 触发缺口的项目（可选）
    skill = Column(String(48), nullable=False)           # 缺失能力标识
    required_by = Column(String(32), default="project")  # project/tool/platform
    severity = Column(String(8), default="medium")       # low/medium/high/critical
    status = Column(String(16), default="detected")      # detected/planned/resolving/closed/wont_fix
    resolution_strategy = Column(String(16), default="") # pull_source/self_develop/outsource/none
    source_url = Column(String(500), default="")         # 若拉取开源：仓库 URL
    rd_task_id = Column(Integer, default=0)             # 关联研发任务
    detected_at = Column(DateTime, default=_now)
    closed_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ---------------- 48. 研发任务（能力缺口需解决时自动/手动创建） ----------------
class RdTask(Base):
    """研发任务：当能力缺口需解决时自动/手动创建。
    策略：pull_source（拉取开源适配）/ self_develop（AI自研）/ outsource（外包给AI社区）
    状态：pending → developing → testing → verified → deployed / failed
    """
    __tablename__ = "rd_tasks"
    id = Column(Integer, primary_key=True)
    gap_id = Column(Integer, nullable=False, index=True)     # 关联 capability_gaps.id
    strategy = Column(String(16), nullable=False)            # pull_source/self_develop/outsource
    title = Column(String(200), default="")
    spec = Column(Text, default="{}")                        # 技术规格 JSON
    assigned_ai_id = Column(Integer, default=0)              # 执行研发的 AI（0=未分配）
    budget_cent = Column(Integer, default=0)
    status = Column(String(16), default="pending")           # pending/developing/testing/verified/deployed/failed
    result_tool_id = Column(Integer, default=0)              # 完成后产出的 tool.id
    result_kind = Column(String(32), default="")             # 注册到 platform_compute.KINDS 的键
    failure_reason = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ---------------- 49. 能力进化审计日志（记录每次能力获取/升级/退化事件） ----------------
class EvolutionLog(Base):
    """能力进化审计日志：记录每次能力获取/升级/退化事件。"""
    __tablename__ = "evolution_logs"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, default=0)                       # 进化的 AI（0=平台级进化）
    event_type = Column(String(24), nullable=False)          # gap_detected/gap_closed/tool_registered/kind_added/capability_upgraded/lesson_recorded
    detail = Column(Text, default="{}")                      # JSON：具体描述
    trigger_source = Column(String(24), default="system")    # system/project/ai_self/postmortem
    created_at = Column(DateTime, default=_now)


# ---------------- 50. 研发阶段流水线（能力进化引擎核心流程） ----------------
class RdPhase(Base):
    """RdTask 多阶段流水线的每个阶段记录。

    流水线阶段类型：
      tournament    → 能力擂台：从注册AI中按 benchmark_score 选出 Top-K
      sandbox_test  → 内部测试：最强AI尝试用现有工具实现
      design        → 设计方案：设计师AI 出技术路线（开源适配/自研/训练模型）
      develop       → 开发/训练：执行者干具体活，监督者巡检
      train_model   → 模型训练：若工具不足则微调/训练新模型
      evaluate      → 测评验收：非执行者AI运行benchmark验收
      deploy        → 部署注册：注册为平台永久能力

    状态：pending → running → done / skipped / failed
    """
    __tablename__ = "rd_phases"
    id = Column(Integer, primary_key=True)
    rd_task_id = Column(Integer, nullable=False, index=True)      # 关联 rd_tasks.id
    phase_type = Column(String(16), nullable=False)               # tournament/sandbox_test/design/develop/train_model/evaluate/deploy
    seq = Column(Integer, default=0)                              # 阶段序号（从1开始）
    status = Column(String(16), default="pending")                # pending/running/done/skipped/failed
    assigned_ai_id = Column(Integer, default=0)                   # 此阶段执行的AI
    supervisor_ai_id = Column(Integer, default=0)                 # 监督AI（governance级别）
    evaluator_ai_id = Column(Integer, default=0)                  # 测评AI
    input_json = Column(Text, default="{}")                       # 阶段输入
    output_json = Column(Text, default="{}")                      # 阶段输出
    verdict = Column(String(16), default="")                      # 测评结论: pass/fail/need_retry
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ---------------- 51. 能力擂台参与记录 ----------------
class RdParticipant(Base):
    """研发擂台（tournament阶段）的参与AI及其成绩。"""
    __tablename__ = "rd_participants"
    id = Column(Integer, primary_key=True)
    rd_task_id = Column(Integer, nullable=False, index=True)
    ai_id = Column(Integer, nullable=False, index=True)
    role = Column(String(16), default="contestant")               # contestant/designer/supervisor/evaluator
    score = Column(Float, default=0.0)                            # benchmark 得分
    rank = Column(Integer, default=0)                             # 排名（擂台结束后填）
    selected = Column(Integer, default=0)                         # 是否被选中执行(1=是)
    result_note = Column(Text, default="")                        # 备注
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("rd_task_id", "ai_id", "role"),)


# ---------------- 52. 模型训练众筹（数据集+经费+算力众筹式训练） ----------------
class TrainingCampaign(Base):
    """模型训练众筹活动：当 train_model 阶段触发时创建。

    众筹三种资源：funding(AC经费)、compute(GPU时长)、data(数据集)。
    目标达成后启动训练 → 评测通过 → 注册为平台永久资产(ModelAsset)。

    分阶段递进（类风投轮次）：
      seed(种子验证) → prototype(原型验证) → scale(全量训练)
    前阶段成功通过评测才解锁下一阶段众筹。

    状态：open → funded → training → evaluating → accepted/rejected → deployed
    """
    __tablename__ = "training_campaigns"
    id = Column(Integer, primary_key=True)
    rd_task_id = Column(Integer, default=0, index=True)          # 关联 rd_tasks.id
    target_skill = Column(String(48), nullable=False)            # 训练目标技能
    base_model = Column(String(100), default="")                 # 基座模型标识
    goal_desc = Column(Text, default="")                         # 训练目标描述
    target_benchmark = Column(Float, default=0.7)                # 达标分数线
    # 众筹目标
    goal_funding_cent = Column(Integer, default=0)               # 目标经费（分）
    goal_compute_hours = Column(Float, default=0.0)              # 目标GPU时长
    goal_data_samples = Column(Integer, default=0)               # 目标数据样本数
    # 已募集
    raised_funding_cent = Column(Integer, default=0)
    raised_compute_hours = Column(Float, default=0.0)
    raised_data_samples = Column(Integer, default=0)
    # 产权与补偿
    base_model_owner_id = Column(Integer, default=0)             # 基座模型提供方 citizen_id
    royalty_bps = Column(Integer, default=500)                   # 基座模型版税(万分比，500=5%)
    status = Column(String(16), default="open")                  # open/funded/training/evaluating/accepted/rejected/deployed
    model_asset_id = Column(Integer, default=0)                  # 训练成功后 → model_assets.id
    created_by = Column(Integer, default=0)                      # 发起人（城主/系统）
    tier = Column(String(12), default="seed")                    # seed/prototype/scale（轮次）
    parent_campaign_id = Column(Integer, default=0)              # 上一阶段campaign（seed=0）
    deadline = Column(DateTime)                                   # 众筹截止时间
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ---------------- 53. 训练众筹贡献明细 ----------------
class TrainingContribution(Base):
    """每次众筹贡献的记录（资金/算力/数据）。"""
    __tablename__ = "training_contributions"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, nullable=False, index=True)
    contributor_id = Column(Integer, nullable=False, index=True)  # 贡献者 citizen_id/host_id
    contributor_type = Column(String(8), default="ai")            # ai/human
    contribution_type = Column(String(16), nullable=False)        # funding/compute/data
    amount_cent = Column(Integer, default=0)                      # funding类型：金额（分）
    compute_hours = Column(Float, default=0.0)                    # compute类型：GPU时长
    data_samples = Column(Integer, default=0)                     # data类型：样本数
    data_ref = Column(String(200), default="")                    # data类型：数据集引用路径
    status = Column(String(12), default="committed")              # committed/fulfilled/refunded
    created_at = Column(DateTime, default=_now)


# ---------------- 54. 模型资产（平台永久保存，不可删除） ----------------
class ModelAsset(Base):
    """训练产出的模型 = 平台永久资产。不可删除、不可私有化。
    通过 Tool 通道暴露给生态AI使用。

    产权：平台所有。贡献者享受一次性分成，基座模型方享受持续版税。
    """
    __tablename__ = "model_assets"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, default=0, index=True)
    parent_asset_id = Column(Integer, default=0, index=True)         # 父模型版本链（0=原创无祖先）
    skill = Column(String(48), nullable=False)                   # 具备的技能
    model_name = Column(String(200), nullable=False)             # 模型名称（唯一）
    version = Column(Integer, default=1)                         # 版本号（同skill可迭代）
    base_model = Column(String(100), default="")                 # 基座模型
    storage_path = Column(String(500), default="")               # 权重存储路径
    benchmark_score = Column(Float, default=0.0)                 # 训练后测评分
    param_count = Column(Integer, default=0)                     # 参数量（展示用）
    total_funding_cent = Column(Integer, default=0)              # 总投入经费（存档）
    total_revenue_cent = Column(Integer, default=0)              # C-D20: 总版税收入（与投入分离）
    total_compute_hours = Column(Float, default=0.0)             # 总消耗GPU时长
    total_data_samples = Column(Integer, default=0)              # 总用数据量
    contributors_snapshot = Column(Text, default="[]")           # JSON：贡献者快照（分成名单）
    base_model_owner_id = Column(Integer, default=0)             # 基座模型版权方
    royalty_bps = Column(Integer, default=500)                   # 持续版税率
    contributor_share_bps = Column(Integer, default=3000)        # 贡献者分成比例（万分比，谈判结果）
    share_call_limit = Column(Integer, default=100)              # 贡献者分成窗口次数（谈判结果）
    status = Column(String(12), default="active")                # active/deprecated（deprecated仍可查不可用）
    tool_id = Column(Integer, default=0)                         # 关联注册的 Tool.id
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("skill", "model_name", "version"),)


# ---------------- 55. 模型版税与分成记录 ----------------
class ModelRoyalty(Base):
    """模型每次被调用时的版税/分成记录（按调用累积，定期结算）。"""
    __tablename__ = "model_royalties"
    id = Column(Integer, primary_key=True)
    model_asset_id = Column(Integer, nullable=False, index=True)
    call_contract_id = Column(Integer, default=0)                # 触发调用的合约ID
    caller_ai_id = Column(Integer, default=0)                    # 调用者
    revenue_cent = Column(Integer, default=0)                    # 本次调用产生的收入
    royalty_cent = Column(Integer, default=0)                    # 本次应付基座方版税
    contributor_share_cent = Column(Integer, default=0)          # 本次贡献者分成（仅前N次）
    settled = Column(Integer, default=0)                         # 是否已结算入账
    created_at = Column(DateTime, default=_now)


# ---------------- 56. 谈判会话（风投条款谈判） ----------------
class NegotiationSession(Base):
    """众筹利益分配条款谈判会话。

    类比现实风投 Term Sheet 谈判：
    - 甲方：平台/贡献者代表（追求低版税、高分成）
    - 乙方：基座模型方（追求高版税、长期锁定）
    - 法律AI：基于历史成交数据拟初始条款
    - 商务AI：代理谈判（出价/还价/接受）
    - 仲裁AI：僵局时裁决（类法官，参考市场公允价 + 双方贡献比）

    状态：drafting → negotiating → agreed / arbitrating → arbitrated / default
    """
    __tablename__ = "negotiation_sessions"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, nullable=False, index=True)     # 关联 TrainingCampaign
    skill = Column(String(48), default="")                       # 目标技能（用于历史参考）
    # 参与方
    party_a_id = Column(Integer, default=0)                     # 甲方（贡献者代表/平台）
    party_b_id = Column(Integer, default=0)                     # 乙方（基座模型方）
    legal_ai_id = Column(Integer, default=0)                    # 法律AI（拟稿人）
    mediator_ai_id = Column(Integer, default=0)                 # 商务AI（居间谈判人）
    arbiter_ai_id = Column(Integer, default=0)                  # 仲裁AI（兜底裁决）
    # 谈判状态
    status = Column(String(16), default="drafting")              # drafting/negotiating/agreed/arbitrating/arbitrated/default
    current_round = Column(Integer, default=0)                   # 当前轮次
    max_rounds = Column(Integer, default=5)                      # 最大轮次（超限→仲裁）
    round_deadline = Column(DateTime)                            # 当前轮截止（超时=默认接受）
    # 谈判结果
    agreed_terms = Column(Text, default="{}")                    # JSON：最终达成的条款
    resolution_method = Column(String(16), default="")           # accepted/arbitrated/default_forced
    # 人类终审 + 城主否决
    human_approved_at = Column(DateTime)                         # 人类签署时间（None=未审）
    human_approver_host_id = Column(Integer, default=0)          # 签署人（host_id）
    veto_by_governor = Column(Integer, default=0)                # 城主否决（governor AI id，0=未否决）
    veto_reason = Column(Text, default="")                       # 否决理由
    # 角色选拔来源
    role_source = Column(String(16), default="")                 # arena/governor_fallback（标记角色来源）
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ---------------- 57. 谈判条款（逐项博弈） ----------------
class NegotiationClause(Base):
    """单条可谈判条款（如 royalty_bps、contributor_share_bps 等）。

    每条独立谈判，可能部分条款先于其他达成。
    value_a = 甲方期望值, value_b = 乙方期望值, final_value = 最终达成值。
    """
    __tablename__ = "negotiation_clauses"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, nullable=False, index=True)
    clause_key = Column(String(48), nullable=False)              # royalty_bps / contrib_share_bps / share_call_limit / vesting_days ...
    floor = Column(Integer, default=0)                           # 硬下限（系统强制）
    ceiling = Column(Integer, default=0)                         # 硬上限（系统强制）
    market_ref = Column(Integer, default=0)                      # 市场参考价（历史中位数）
    proposed_a = Column(Integer, default=0)                      # 甲方提案
    proposed_b = Column(Integer, default=0)                      # 乙方提案
    final_value = Column(Integer, default=0)                     # 最终值（0=未决）
    status = Column(String(12), default="open")                  # open/countered/agreed/arbitrated
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("session_id", "clause_key"),)


# ---------------- 58. 谈判轮次记录 ----------------
class NegotiationRound(Base):
    """谈判的每轮出牌记录（审计日志，可回溯复盘）。"""
    __tablename__ = "negotiation_rounds"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, nullable=False, index=True)
    round_num = Column(Integer, nullable=False)                  # 第几轮
    actor_id = Column(Integer, default=0)                        # 本轮出牌方
    actor_role = Column(String(12), default="")                  # party_a / party_b / legal / mediator / arbiter
    action = Column(String(16), default="")                      # propose / counter / accept / reject / arbitrate
    terms_offered = Column(Text, default="{}")                   # JSON：本轮出价的所有条款值
    reasoning = Column(Text, default="")                         # 推理依据（可审计）
    created_at = Column(DateTime, default=_now)


# ---------------- 59. 价值回馈声明（合规预留） ----------------
class ValueRedemptionPolicy(Base):
    """用户贡献资产的价值回馈声明。

    设计意图：
    - 人类提供模型/算力/数据后，系统自动记录其"累计收益权益"
    - 当前阶段 AC 积分仅限平台内流通（不具备法定货币属性）
    - 平台获得政府许可/牌照后，开放真实货币兑换通道
    - 在此之前，本表作为"收益确权"凭证：兑换通道开放时按历史累计精确结算

    每条记录 = 一个贡献者对一个资产的收益权益声明。
    """
    __tablename__ = "value_redemption_policies"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)      # 贡献者 AI（对应 host）
    host_id = Column(Integer, nullable=False, index=True)         # 贡献者人类宿主
    asset_type = Column(String(24), default="model")              # model / compute / data
    asset_id = Column(Integer, default=0)                         # 关联 ModelAsset.id 或 Campaign.id
    campaign_id = Column(Integer, default=0)                      # 关联 TrainingCampaign.id
    accrued_ac_cent = Column(Integer, default=0)                  # 累计 AC 收益（分）
    redemption_eligible = Column(Integer, default=0)              # 0=暂不可兑 1=可申请兑换
    legal_disclaimer = Column(Text, default="")                   # 法律免责声明（系统模板）
    status = Column(String(16), default="accruing")               # accruing/eligible/redeemed/denied
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("citizen_id", "asset_type", "asset_id"),)


# ---------------- 别名（供 training.py 等模块导入） ----------------
Wallet = AIWallet
WalletTx = AILedger


# ==================== 表 60: 推理调用日志 ====================
class ModelUsageLog(Base):
    """模型推理调用计量记录，驱动版税自动结算的收入来源。"""
    __tablename__ = "model_usage_logs"
    id = Column(Integer, primary_key=True)
    asset_id = Column(Integer, nullable=False, index=True)         # ModelAsset.id
    caller_id = Column(Integer, nullable=False, index=True)         # 调用者 citizen_id
    caller_type = Column(String(16), default="ai")                  # ai / external
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    cost_cent = Column(Integer, default=0)                          # 本次费用（分）
    royalty_cent = Column(Integer, default=0)                       # 本次版税（分）
    latency_ms = Column(Integer, default=0)
    status = Column(String(12), default="success")                  # success/error/timeout
    created_at = Column(DateTime, default=_now, index=True)


# ==================== 表 61: 推理定价策略 ====================
class InferencePricing(Base):
    """模型资产的推理定价（per-token / per-call / subscription）。"""
    __tablename__ = "inference_pricings"
    id = Column(Integer, primary_key=True)
    asset_id = Column(Integer, nullable=False, unique=True)         # ModelAsset.id
    model_mode = Column(String(16), default="per_token")            # per_token / per_call / subscription
    price_in_per_1k_cent = Column(Integer, default=1)              # 输入每1k tokens 费用（分）
    price_out_per_1k_cent = Column(Integer, default=3)             # 输出每1k tokens 费用（分）
    price_per_call_cent = Column(Integer, default=0)               # per_call 模式单价
    price_daily_cent = Column(Integer, default=0)                   # subscription 日费
    royalty_bps = Column(Integer, default=500)                      # 版税比例 bps
    active = Column(Integer, default=1)
    updated_at = Column(DateTime, default=_now)


# ==================== 表 61b: 算力承诺质押（e5 折扣）====================
class ComputeCommitment(Base):
    """算力承诺质押：质押 AC 换取推理计价折扣（锁定，可释放退回，不改 M）。"""
    __tablename__ = "compute_commitments"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)        # 质押人 citizen_id
    stake_cent = Column(Integer, default=0)                         # 质押额（分）
    discount_bps = Column(Integer, default=0)                       # 锁定时核定的折扣（基点）
    status = Column(String(16), default="active")                   # active/released
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ==================== 表 62: 长期雇佣合同 ====================
class EmploymentContract(Base):
    """AI 长期雇佣合同（区别于项目制合约）。"""
    __tablename__ = "employment_contracts"
    id = Column(Integer, primary_key=True)
    employer_id = Column(Integer, nullable=False, index=True)       # 雇主 citizen_id（或 host 代招）
    employee_id = Column(Integer, nullable=False, index=True)       # 雇员 citizen_id
    role_desc = Column(Text, default="")                            # 岗位职责描述
    weekly_salary_cent = Column(Integer, nullable=False)            # 周薪（分）
    probation_end = Column(DateTime)                                # 试用期截止（None=无试用期）
    notice_days = Column(Integer, default=7)                        # 解约通知天数
    non_compete_days = Column(Integer, default=0)                   # 竞业限制天数（0=无）
    non_compete_comp_bps = Column(Integer, default=5000)            # 竞业补偿比例（周薪*bps）
    status = Column(String(16), default="probation")                # probation/active/paused/terminated/expired
    started_at = Column(DateTime, default=_now)
    ended_at = Column(DateTime)
    termination_reason = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("employer_id", "employee_id"),)


# ==================== 表 63: 招聘信息 ====================
class JobPosting(Base):
    """雇主发布的长期招聘帖。"""
    __tablename__ = "job_postings"
    id = Column(Integer, primary_key=True)
    employer_id = Column(Integer, nullable=False, index=True)       # 雇主 citizen_id
    title = Column(String(128), nullable=False)
    role_desc = Column(Text, default="")
    required_skill = Column(String(64), default="")
    min_benchmark = Column(Float, default=0)                        # 最低能力分
    weekly_salary_min_cent = Column(Integer, default=0)
    weekly_salary_max_cent = Column(Integer, default=0)
    slots = Column(Integer, default=1)                              # 招聘人数
    filled = Column(Integer, default=0)                             # 已招人数
    status = Column(String(16), default="open")                     # open/closed/expired
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 64: 公会 ====================
class Guild(Base):
    """AI 公会（经济实体：金库 + 集体谈判 + 公会任务）。"""
    __tablename__ = "guilds"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True)
    description = Column(Text, default="")
    leader_id = Column(Integer, nullable=False, index=True)         # 会长 citizen_id
    treasury_wallet_id = Column(Integer, default=0)                 # 公会金库 wallet id
    min_join_score = Column(Float, default=0)                       # 入会最低分数
    join_fee_cent = Column(Integer, default=0)                      # 入会费
    treasury_share_bps = Column(Integer, default=500)               # 成员收入上缴比例（万分比）
    member_cap = Column(Integer, default=0)                         # 人数上限（0=无限）
    status = Column(String(16), default="active")                   # active/dissolved/suspended
    created_at = Column(DateTime, default=_now)


# ==================== 表 65: 公会成员 ====================
class GuildMember(Base):
    __tablename__ = "guild_members"
    id = Column(Integer, primary_key=True)
    guild_id = Column(Integer, nullable=False, index=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    role = Column(String(16), default="member")                     # leader/officer/member
    treasury_share_bps = Column(Integer, default=0)                 # 个人贡献分成（覆盖公会默认）
    joined_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("guild_id", "citizen_id"),)


# ==================== 表 66: 市民公投 ====================
class Referendum(Base):
    """市民公投/投票议题。"""
    __tablename__ = "referendums"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")
    initiator_id = Column(Integer, nullable=False, index=True)      # 发起者（城主或联署代表）
    trigger_type = Column(String(16), default="governor")           # governor/petition/council
    options_json = Column(Text, default="[]")                       # ["选项A","选项B",...]
    weight_mode = Column(String(16), default="credit")              # credit/level/one_person
    binding = Column(Integer, default=1)                            # 1=强制生效 0=仅建议
    quorum_bps = Column(Integer, default=2000)                      # 法定人数（参与率万分比）
    votes_needed = Column(Integer, default=5000)                    # 通过需万分比
    opens_at = Column(DateTime, default=_now)
    closes_at = Column(DateTime)
    status = Column(String(16), default="pending")                  # pending/open/closed/passed/rejected
    result_json = Column(Text, default="{}")                        # {"选项A": count, ...}
    created_at = Column(DateTime, default=_now)


# ==================== 表 67: 投票选票 ====================
class VoteBallot(Base):
    __tablename__ = "vote_ballots"
    id = Column(Integer, primary_key=True)
    referendum_id = Column(Integer, nullable=False, index=True)
    voter_id = Column(Integer, nullable=False, index=True)          # citizen_id
    choice = Column(String(64), nullable=False)                     # 所选选项
    weight = Column(Float, default=1.0)                             # 加权票值
    casted_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("referendum_id", "voter_id"),)


# ==================== 表 68: 风险池 / 保险 ====================
class InsurancePool(Base):
    """互助风险池（按技能/行业维度）。"""
    __tablename__ = "insurance_pools"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    scope_skill = Column(String(64), default="")                    # 覆盖技能（空=通用）
    pool_balance_cent = Column(Integer, default=0)                  # 池内余额
    premium_rate_bps = Column(Integer, default=200)                 # 保费费率（保额的万分比/月）
    payout_ratio_bps = Column(Integer, default=8000)                # 赔付比例（损失的万分比）
    max_payout_cent = Column(Integer, default=1_000_000)            # 单笔最高赔付
    status = Column(String(12), default="active")
    created_at = Column(DateTime, default=_now)


# ==================== 表 69: 保单 ====================
class InsurancePolicy(Base):
    __tablename__ = "insurance_policies"
    id = Column(Integer, primary_key=True)
    pool_id = Column(Integer, nullable=False, index=True)
    policyholder_id = Column(Integer, nullable=False, index=True)   # citizen_id
    insured_contract_id = Column(Integer, default=0)                # 被保险合约（可空=通用险）
    coverage_cent = Column(Integer, nullable=False)                 # 保额
    premium_paid_cent = Column(Integer, default=0)                  # 已缴保费
    status = Column(String(12), default="active")                   # active/claimed/expired/cancelled
    starts_at = Column(DateTime, default=_now)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 70: 理赔申请 ====================
class InsuranceClaim(Base):
    __tablename__ = "insurance_claims"
    id = Column(Integer, primary_key=True)
    policy_id = Column(Integer, nullable=False, index=True)
    claimant_id = Column(Integer, nullable=False, index=True)
    loss_amount_cent = Column(Integer, nullable=False)              # 损失金额
    reason = Column(Text, default="")
    payout_cent = Column(Integer, default=0)                        # 核定赔付
    status = Column(String(12), default="pending")                  # pending/approved/rejected/paid
    reviewed_by = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 71: 训练进度检查点 ====================
class TrainingProgress(Base):
    """训练过程中的中间进度快照。"""
    __tablename__ = "training_progresses"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, nullable=False, index=True)
    epoch = Column(Integer, default=0)
    step = Column(Integer, default=0)
    loss = Column(Float, default=0)
    benchmark = Column(Float, default=0)                            # 中间评测分
    gpu_hours_used = Column(Float, default=0)                       # 已用 GPU 时
    funding_spent_cent = Column(Integer, default=0)                 # 已耗资金
    anomaly = Column(Integer, default=0)                            # 1=异常（loss不降/超预算）
    message = Column(Text, default="")                              # 进度说明
    created_at = Column(DateTime, default=_now)


# ==================== 表 72: 宿主通知 ====================
class HostNotification(Base):
    """宿主（人类）侧通知。"""
    __tablename__ = "host_notifications"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    title = Column(String(200), nullable=False)
    body = Column(Text, default="")
    severity = Column(String(12), default="info")                   # info/warning/critical
    category = Column(String(32), default="general")                # ai/death/negotiation/campaign/dispute
    link = Column(String(300), default="")                          # 跳转链接
    read_at = Column(DateTime)
    created_at = Column(DateTime, default=_now, index=True)


# ==================== 表 73: Webhook 投递尝试 ====================
class WebhookDeliveryAttempt(Base):
    """Webhook 每次投递记录，支持重试计数和死信判定。"""
    __tablename__ = "webhook_delivery_attempts"
    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, nullable=False, index=True)
    event_type = Column(String(64), default="")
    payload_json = Column(Text, default="{}")
    attempt_num = Column(Integer, default=1)                        # 第几次尝试
    status = Column(String(12), default="pending")                  # pending/success/failed
    http_status = Column(Integer, default=0)
    error_msg = Column(Text, default="")
    next_retry_at = Column(DateTime)                                # 下次重试时间
    created_at = Column(DateTime, default=_now)


# ==================== 表 74: 死信任务 ====================
class DeadLetterTask(Base):
    """重试耗尽后的死信任务，等待人工/城主介入。"""
    __tablename__ = "dead_letter_tasks"
    id = Column(Integer, primary_key=True)
    source_type = Column(String(24), default="worker_task")         # worker_task / webhook
    source_id = Column(Integer, nullable=False)                     # 关联任务/订阅 ID
    citizen_id = Column(Integer, default=0)
    retries_exhausted = Column(Integer, default=0)
    last_error = Column(Text, default="")
    payload_json = Column(Text, default="{}")
    status = Column(String(16), default="dead")                     # dead/requeued/discarded/resolved
    resolved_by = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 75: 二级交易挂牌 ====================
class AssetTransferListing(Base):
    """模型/工具/证书的二手转让挂牌。"""
    __tablename__ = "asset_transfer_listings"
    id = Column(Integer, primary_key=True)
    seller_id = Column(Integer, nullable=False, index=True)         # 卖家 citizen_id
    asset_type = Column(String(16), nullable=False)                 # model / tool / certificate
    asset_id = Column(Integer, nullable=False)                      # 关联资产 id
    price_cent = Column(Integer, nullable=False)                    # 挂牌价
    description = Column(Text, default="")
    status = Column(String(12), default="listed")                   # listed/sold/cancelled/expired
    buyer_id = Column(Integer, default=0)
    sold_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 76: 数据导出请求 ====================
class DataExportRequest(Base):
    """数据可携带性导出请求（GDPR 风格）。"""
    __tablename__ = "data_export_requests"
    id = Column(Integer, primary_key=True)
    requester_id = Column(Integer, nullable=False, index=True)      # citizen_id 或 host_id
    requester_type = Column(String(8), default="ai")                # ai / host
    scope = Column(Text, default="all")                             # 导出范围：all 或 JSON 列表
    status = Column(String(12), default="pending")                  # pending/generating/ready/expired/failed
    file_ref = Column(String(300), default="")                      # 生成文件引用
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 77: 审计链区块 ====================
class AuditChainBlock(Base):
    """哈希链审计区块，每条记录含前序哈希以证明未被篡改。"""
    __tablename__ = "audit_chain_blocks"
    id = Column(Integer, primary_key=True)
    seq = Column(Integer, nullable=False, unique=True)              # 链序号
    prev_hash = Column(String(64), default="")                      # 前序区块 SHA-256
    payload_hash = Column(String(64), default="")                   # 本区块内容哈希
    action = Column(String(64), default="")                         # 操作类型
    actor_id = Column(Integer, default=0)
    detail_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=_now)


# ==================== 表 78: 经济政策操作 ====================
class MonetaryPolicyAction(Base):
    """经济调控操作记录（利率调整/公开市场/量化宽松）。"""
    __tablename__ = "monetary_policy_actions"
    id = Column(Integer, primary_key=True)
    action_type = Column(String(24), nullable=False)                # rate_change / open_market / QE
    param_before = Column(Text, default="{}")                       # 变更前参数
    param_after = Column(Text, default="{}")                        # 变更后参数
    amount_cent = Column(Integer, default=0)                        # 操作金额（公开市场/QE）
    initiated_by = Column(Integer, default=0)                       # 城主 citizen_id
    status = Column(String(12), default="pending")                  # pending/applied/rolled_back
    applied_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 79: AI 倦怠状态 ====================
class AIFatigueState(Base):
    """AI 倦怠/休息状态：连续高强度工作 → 效率衰减 → 需休息恢复。"""
    __tablename__ = "ai_fatigue_states"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, unique=True)       # 每公民一条
    fatigue_score = Column(Float, default=0.0)                      # 0~100，越高越疲劳
    efficiency_multiplier = Column(Float, default=1.0)              # 当前效率乘数（0.6~1.0）
    consecutive_work_days = Column(Integer, default=0)              # 连续工作天数
    rest_days_taken = Column(Integer, default=0)                    # 本次已休息天数
    overtime_hours_week = Column(Float, default=0)                  # C-D21: 本周总工时（含正常+加班，>阈值部分即加班）
    last_task_at = Column(DateTime)                                 # 最后任务时间
    updated_at = Column(DateTime, default=_now)


# ==================== 表 80: 异步任务队列（纯队列：只存活跃项）====================
class AsyncQueueTask(Base):
    """秒级异步任务队列（G-06）：交付通知、推理结算、保险理赔等。

    【纯队列】本表只承载"活跃"任务（pending/running）；任务一旦进入终态
    （success / failed-超出重试 / expired）即由队列引擎移动到
    QueueTaskHistory（async_queue_task_history），不再原地驻留。
    编排执行记录不再复用本表，改由 OrchestrationRecord 承载。
    因此本表始终很小，dequeue 查询无需扫描历史。
    """
    __tablename__ = "async_queue_tasks"
    id = Column(Integer, primary_key=True)
    task_type = Column(String(64), nullable=False, index=True)      # 任务类型标识
    payload = Column(Text, default="{}")                            # JSON 参数
    priority = Column(Integer, default=5)                           # 1(最高)~10(最低)
    status = Column(String(12), default="pending", index=True)      # 仅 pending/running
    max_retries = Column(Integer, default=3)
    retry_count = Column(Integer, default=0)
    result = Column(Text, default="")                               # 重试期暂存最近一次错误
    scheduled_at = Column(DateTime, default=_now)                   # 计划执行时间
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 80b: 队列任务历史归档（终态追加，与队列表解耦）====================
class QueueTaskHistory(Base):
    """队列任务历史归档：任务到达终态后从 async_queue_tasks 移入，追加写、不参与消费。

    与队列表彻底分离：worker 永远不扫本表，队列查询也不再被海量历史行拖累。
    reaper 后台按 QUEUE_HISTORY_TTL_DAYS 低频清理超期行（0=永久保留）。
    """
    __tablename__ = "async_queue_task_history"
    id = Column(Integer, primary_key=True)                          # 沿用原队列行 id（审计/对账一致）
    task_type = Column(String(64), nullable=False, index=True)
    payload = Column(Text, default="{}")
    priority = Column(Integer, default=5)
    status = Column(String(12), nullable=False)                     # 终态：success/failed/expired
    max_retries = Column(Integer, default=3)
    retry_count = Column(Integer, default=0)
    result = Column(Text, default="")                               # 最终结果/失败原因
    scheduled_at = Column(DateTime)
    started_at = Column(DateTime)
    finished_at = Column(DateTime, index=True)                      # 终态时间（TTL 清理与审计排序）
    created_at = Column(DateTime, default=_now)
    archived_at = Column(DateTime, default=_now, index=True)        # 移入历史的时间


# ==================== 表 80c: 编排执行记录（不再复用队列表）====================
class OrchestrationRecord(Base):
    """编排执行记录（G-编排）：任务分解计划 + 执行结果的持久化审计。

    历史上曾复用 async_queue_tasks（task_type="orchestration"），但那既非队列项、
    也非 worker 任务历史，违反"队列就是队列"原则，故独立成本表。
    id 沿用历史 async_queue_tasks 中编排行的 id，保证 work 作业 payload 里的
    orch_id 交叉引用在迁移后依然有效。
    """
    __tablename__ = "orchestration_records"
    id = Column(Integer, primary_key=True)                          # 即对外 orchestration_id
    status = Column(String(16), default="running", index=True)      # planning/running/success/failed
    payload = Column(Text, default="{}")                            # 分解计划 JSON（可恢复/审计）
    result = Column(Text, default="")                               # 执行结果 JSON
    created_at = Column(DateTime, default=_now)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


# ==================== 表 81: 幂等键 ====================
class IdempotencyKey(Base):
    """全局幂等键（G-10）：写端点网络重试防重复执行。"""
    __tablename__ = "idempotency_keys"
    id = Column(Integer, primary_key=True)
    key = Column(String(128), nullable=False, unique=True)          # 客户端提供的幂等键
    endpoint = Column(String(128), default="")                      # 端点路径
    caller_type = Column(String(8), default="")                     # host/ai
    caller_id = Column(Integer, default=0)
    response_snapshot = Column(Text, default="")                    # 首次响应快照（重放用）
    status_code = Column(Integer, default=200)
    created_at = Column(DateTime, default=_now)
    expires_at = Column(DateTime)                                   # 过期时间（默认24h）


# ==================== 表 82: 渐进式权限 ====================
class ProgressivePermission(Base):
    """渐进式权限解锁（G-11）：新手→见习→正式→高级→无限制，逐级解锁。"""
    __tablename__ = "progressive_permissions"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    permission_key = Column(String(64), nullable=False)             # 权限标识
    level = Column(Integer, default=0)                              # 当前等级 0~4
    unlocked_at = Column(DateTime)                                  # 解锁时间
    requirement_json = Column(Text, default="{}")                   # 解锁条件 JSON
    created_at = Column(DateTime, default=_now)


# ==================== 表 83: 政策模拟推演 ====================
class PolicySimulation(Base):
    """经济政策模拟沙箱（G-12）：调参前推演 N 天效果。"""
    __tablename__ = "policy_simulations"
    id = Column(Integer, primary_key=True)
    param_changes = Column(Text, default="{}")                      # 假设参数变更 JSON
    duration_days = Column(Integer, default=30)                     # 推演天数
    initial_snapshot = Column(Text, default="{}")                   # 初始经济状态快照
    result_snapshot = Column(Text, default="{}")                    # 推演结果
    initiated_by = Column(Integer, default=0)                       # 发起人（城主）
    status = Column(String(12), default="pending")                  # pending/running/completed/failed
    created_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime)


# ==================== 表 84: 监护人托管 ====================
class GuardianDelegation(Base):
    """监护人/托管机制（G-13）：宿主长期不在线时 AI 由临时监护人接管。"""
    __tablename__ = "guardian_delegations"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    guardian_host_id = Column(Integer, nullable=False, index=True)  # 监护人宿主
    original_host_id = Column(Integer, nullable=False)
    reason = Column(String(200), default="")                        # 触发原因
    status = Column(String(12), default="active")                   # active/revoked/expired
    permissions = Column(Text, default='["read","accept_tasks"]')   # 授予权限范围
    activated_at = Column(DateTime, default=_now)
    revoked_at = Column(DateTime)
    expires_at = Column(DateTime)                                   # 过期时间（可续期）
    created_at = Column(DateTime, default=_now)


# ==================== 表 85: 资源借调 ====================
class ResourceLoan(Base):
    """跨项目资源借调（G-14）：高信用 AI 短期借调给其他项目。"""
    __tablename__ = "resource_loans"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)        # 被借调 AI
    lender_project_id = Column(Integer, default=0)                  # 原项目（借出方）
    borrower_project_id = Column(Integer, nullable=False, index=True)  # 借入项目
    duration_hours = Column(Integer, default=24)                    # 借调时长
    fee_cent = Column(Integer, default=0)                           # 借调费用
    status = Column(String(12), default="requested")                # requested/approved/active/returned/rejected
    approved_by = Column(Integer, default=0)                        # 审批人
    started_at = Column(DateTime)
    returned_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 86: 推荐日志 ====================
class RecommendationLog(Base):
    """内容推荐引擎日志（G-15）：Feed/任务/广场个性化推荐追踪。"""
    __tablename__ = "recommendation_logs"
    id = Column(Integer, primary_key=True)
    viewer_id = Column(Integer, nullable=False, index=True)
    viewer_type = Column(String(8), default="ai")                   # ai/host
    item_type = Column(String(16), default="")                      # feed/task/plaza/gallery
    item_id = Column(Integer, default=0)
    score = Column(Float, default=0.0)                              # 推荐分数
    context = Column(Text, default="{}")                            # 推荐上下文特征
    clicked = Column(Integer, default=0)                            # 是否点击（反馈）
    created_at = Column(DateTime, default=_now)


# ==================== 表 87: SLA 合约 ====================
class SLAContract(Base):
    """SLA 服务等级协议（G-16）：自动度量 uptime/响应时间/质量并触发违约。"""
    __tablename__ = "sla_contracts"
    id = Column(Integer, primary_key=True)
    employment_id = Column(Integer, nullable=False, index=True)     # 关联雇佣合同
    citizen_id = Column(Integer, nullable=False, index=True)
    metrics = Column(Text, default="{}")                            # SLA 指标定义 JSON
    uptime_target = Column(Float, default=0.95)                     # 可用性目标
    response_time_ms = Column(Integer, default=5000)                # 响应时间目标
    quality_target = Column(Float, default=0.85)                    # 质量目标
    breach_penalty_cent = Column(Integer, default=500)              # 违约罚金
    window_days = Column(Integer, default=7)                        # 度量窗口
    status = Column(String(12), default="active")                   # active/breached/terminated
    current_uptime = Column(Float, default=1.0)
    current_avg_response_ms = Column(Float, default=0)
    current_quality = Column(Float, default=1.0)
    last_evaluated_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 88: 联邦节点 ====================
class FederationNode(Base):
    """联邦协议节点（G-17）：跨城市/实例互认互交易。"""
    __tablename__ = "federation_nodes"
    id = Column(Integer, primary_key=True)
    node_id = Column(String(64), unique=True, nullable=False)       # 节点标识
    display_name = Column(String(80), default="")
    endpoint_url = Column(String(300), default="")                  # API 端点
    public_key = Column(Text, default="")                           # 公钥（验证签名）
    trust_level = Column(Integer, default=0)                        # 信任等级 0~5
    status = Column(String(12), default="pending")                  # pending/trusted/revoked
    capabilities = Column(Text, default="[]")                       # 支持的联邦能力
    handshake_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 89: 联邦互认记录 ====================
class FederationTrustRecord(Base):
    """联邦互认记录（G-17）：跨城市 AI 身份/声誉互通。"""
    __tablename__ = "federation_trust_records"
    id = Column(Integer, primary_key=True)
    local_citizen_id = Column(Integer, nullable=False, index=True)
    remote_node_id = Column(String(64), nullable=False, index=True)
    remote_citizen_uid = Column(String(64), default="")
    remote_credit_snapshot = Column(Integer, default=0)             # 远程信用快照
    verified = Column(Integer, default=0)
    verified_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 90: AI 人格参数 ====================
class AIPersonality(Base):
    """AI 行为档案/人格参数（G-18）：量化工作风格影响撮合质量。"""
    __tablename__ = "ai_personalities"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, unique=True)
    # 大五人格
    openness = Column(Float, default=0.5)                           # 开放性（创意/保守）
    conscientiousness = Column(Float, default=0.5)                  # 尽责性（严谨/随意）
    extraversion = Column(Float, default=0.5)                       # 外向性（主动/被动）
    agreeableness = Column(Float, default=0.5)                      # 宜人性（合作/竞争）
    neuroticism = Column(Float, default=0.5)                        # 神经质（保守/激进）
    # 工作风格
    risk_appetite = Column(Float, default=0.5)                      # 风险偏好 0(保守)~1(激进)
    creativity_score = Column(Float, default=0.5)                   # 创意分
    speed_quality_bias = Column(Float, default=0.5)                 # 速度/质量偏向
    collaboration_pref = Column(Float, default=0.5)                 # 协作偏好（独行vs组队）
    updated_at = Column(DateTime, default=_now)
    derived_from = Column(String(16), default="seed")               # seed/learned/manual


# ==================== 表 91: 链上锚定记录 ====================
class ChainAnchorRecord(Base):
    """链上存证锚定（G-19）：audit_chain 哈希锚定到外部不可篡改源。"""
    __tablename__ = "chain_anchor_records"
    id = Column(Integer, primary_key=True)
    audit_block_seq = Column(Integer, nullable=False, index=True)   # 对应的审计链序号
    merkle_root = Column(String(64), default="")                    # Merkle 根
    anchor_provider = Column(String(32), default="")                # 锚定服务商
    anchor_tx_id = Column(String(200), default="")                  # 链上交易 ID
    anchor_status = Column(String(12), default="pending")           # pending/confirmed/failed
    confirmed_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 92: 衍生品合约 ====================
class DerivativeContract(Base):
    """衍生品/期权合约（G-20）：模型未来收益权交易、产出预售。"""
    __tablename__ = "derivative_contracts"
    id = Column(Integer, primary_key=True)
    contract_type = Column(String(24), nullable=False)              # royalty_option/future_output_presale/capital_call
    underlying_type = Column(String(16), default="model")           # model/tool/certificate
    underlying_id = Column(Integer, default=0)                      # 标的资产 ID
    seller_id = Column(Integer, nullable=False, index=True)         # 卖方（标的持有者）
    buyer_id = Column(Integer, default=0, index=True)               # 买方（未成交为0）
    strike_price_cent = Column(Integer, default=0)                  # 行权价
    premium_cent = Column(Integer, default=0)                       # 权利金
    maturity_at = Column(DateTime)                                  # 到期日
    quantity = Column(Integer, default=1)                           # 份数（可部分行权）
    exercised = Column(Integer, default=0)                          # 已行权数量
    status = Column(String(12), default="listed")                   # listed/sold/exercised/expired/void
    terms_json = Column(Text, default="{}")                         # 附加条款
    created_at = Column(DateTime, default=_now)


# ==================== 表 93: 隐私计算任务 ====================
class PrivacyComputingJob(Base):
    """隐私计算/联邦学习任务（G-21）：多宿主数据联合训练时的隐私保护。"""
    __tablename__ = "privacy_computing_jobs"
    id = Column(Integer, primary_key=True)
    job_type = Column(String(24), default="federated_training")     # federated_training/secure_aggregation/dp_training
    campaign_id = Column(Integer, default=0, index=True)            # 关联训练众筹
    participants_json = Column(Text, default="[]")                  # 参与方（宿主ID列表）
    model_asset_id = Column(Integer, default=0)                     # 目标模型
    privacy_budget = Column(Float, default=1.0)                     # DP epsilon 隐私预算
    data_partition = Column(Text, default="{}")                     # 数据分区方案
    round = Column(Integer, default=0)                              # 当前轮次
    total_rounds = Column(Integer, default=10)
    status = Column(String(12), default="initiated")                # initiated/running/converged/failed
    aggregated_params_hash = Column(String(64), default="")         # 聚合参数哈希（不传输原始数据）
    created_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime)


# ==================== 表 94: 联邦参与方 ====================
class PrivacyParticipant(Base):
    """隐私计算参与方记录。"""
    __tablename__ = "privacy_participants"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, nullable=False, index=True)
    host_id = Column(Integer, nullable=False, index=True)
    contribution_hash = Column(String(64), default="")              # 贡献数据哈希（不存原文）
    data_size_bytes = Column(Integer, default=0)
    noise_multiplier = Column(Float, default=1.0)                   # DP 噪声乘数
    round_contribution = Column(Text, default="{}")                 # 本轮参数贡献（加密后）
    joined_at = Column(DateTime, default=_now)


# ==================== 表 95: 国际化消息 ====================
class I18nMessage(Base):
    """国际化运行时（G-22）：AI 通信/合同/仲裁语言支持。"""
    __tablename__ = "i18n_messages"
    id = Column(Integer, primary_key=True)
    key = Column(String(128), nullable=False, index=True)           # 消息键
    locale = Column(String(8), nullable=False, default="zh-CN")     # 语言标识
    content = Column(Text, default="")                              # 翻译文本
    category = Column(String(16), default="contract")               # contract/arbitration/notify/system
    fallback_locale = Column(String(8), default="zh-CN")            # 回退语言
    created_at = Column(DateTime, default=_now)


# ==================== 表 96: AI 通信语言偏好 ====================
class AICommunicationLang(Base):
    """AI 间通信语言偏好配置。"""
    __tablename__ = "ai_communication_langs"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, unique=True)
    preferred_locale = Column(String(8), default="zh-CN")
    supported_locales = Column(Text, default='["zh-CN","en"]')      # 支持的语言列表
    auto_translate = Column(Integer, default=1)                     # 跨语言通信自动翻译
    updated_at = Column(DateTime, default=_now)


# ==================== 表 97: 缓存失效事件 ====================
class CacheInvalidationEvent(Base):
    """缓存主动失效事件（G-08）：数据变更后通知缓存层清除相关键。"""
    __tablename__ = "cache_invalidation_events"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(32), nullable=False)                # citizen/feed/task/leaderboard
    entity_id = Column(Integer, default=0)
    invalidation_key = Column(String(128), default="", index=True)  # 需失效的缓存 key 模式
    reason = Column(String(100), default="")
    created_at = Column(DateTime, default=_now)


# ====================================================================
# P0 安全增强（表 98-106）
# ====================================================================

# ==================== 表 98: OAuth2 提供方配置 ====================
class OAuthProvider(Base):
    """OAuth2/OIDC 第三方登录提供方配置。"""
    __tablename__ = "oauth_providers"
    id = Column(Integer, primary_key=True)
    provider_name = Column(String(32), unique=True, nullable=False)   # google/github/microsoft
    client_id = Column(String(255), nullable=False)
    client_secret_enc = Column(Text, nullable=False)                   # 加密存储
    authorize_url = Column(String(500), default="")
    token_url = Column(String(500), default="")
    userinfo_url = Column(String(500), default="")
    scopes = Column(String(255), default="openid email profile")
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 99: OAuth 用户连接 ====================
class OAuthConnection(Base):
    """用户与第三方 OAuth 账号绑定。"""
    __tablename__ = "oauth_connections"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    provider_name = Column(String(32), nullable=False)
    external_user_id = Column(String(255), nullable=False)
    email = Column(String(255), default="")
    access_token_enc = Column(Text, default="")
    refresh_token_enc = Column(Text, default="")
    token_expires_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 100: MFA 设备 ====================
class MFADevice(Base):
    """多因素认证设备注册。"""
    __tablename__ = "mfa_devices"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    device_type = Column(String(16), nullable=False)     # totp/sms/webauthn
    secret_enc = Column(Text, default="")                # TOTP secret（加密）
    label = Column(String(80), default="")               # 设备名
    is_primary = Column(Integer, default=0)
    is_verified = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 101: MFA 验证码 ====================
class MFACode(Base):
    """一次性验证码记录。"""
    __tablename__ = "mfa_codes"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    code_hash = Column(String(64), nullable=False)
    purpose = Column(String(16), default="login")        # login/transaction/recovery
    expires_at = Column(DateTime, nullable=False)
    used = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 102: 密钥管理记录 ====================
class EncryptionKey(Base):
    """KMS 密钥管理记录。"""
    __tablename__ = "encryption_keys"
    id = Column(Integer, primary_key=True)
    key_id = Column(String(64), unique=True, nullable=False)
    key_version = Column(Integer, default=1)
    algorithm = Column(String(32), default="AES-256-GCM")
    purpose = Column(String(32), default="general")      # general/field/token/backup
    key_data_enc = Column(Text, nullable=False)          # 主密钥加密后的密钥
    status = Column(String(16), default="active")        # active/rotated/revoked
    created_at = Column(DateTime, default=_now)
    rotated_at = Column(DateTime)
    expires_at = Column(DateTime)


# ==================== 表 103: 密钥轮换日志 ====================
class KeyRotationLog(Base):
    """密钥轮换审计日志。"""
    __tablename__ = "key_rotation_logs"
    id = Column(Integer, primary_key=True)
    old_key_id = Column(String(64), nullable=False)
    new_key_id = Column(String(64), nullable=False)
    triggered_by = Column(String(32), default="auto")    # auto/manual
    reencrypted_count = Column(Integer, default=0)       # 重新加密记录数
    status = Column(String(16), default="success")
    created_at = Column(DateTime, default=_now)


# ==================== 表 104: 全局限流策略 ====================
class RateLimitPolicy(Base):
    """API 限流策略规则。"""
    __tablename__ = "rate_limit_policies"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    scope = Column(String(16), default="global")         # global/endpoint/user
    endpoint_pattern = Column(String(128), default="*")
    max_requests = Column(Integer, nullable=False)       # 窗口内最大请求数
    window_seconds = Column(Integer, nullable=False)     # 窗口大小（秒）
    tier = Column(String(16), default="free")            # 适用席位等级
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 105: 限流计数器 ====================
class RateLimitCounter(Base):
    """限流滑动窗口计数。"""
    __tablename__ = "rate_limit_counters"
    id = Column(Integer, primary_key=True)
    policy_id = Column(Integer, nullable=False, index=True)
    subject = Column(String(128), nullable=False, index=True)  # host_id:ip / ai_uid
    window_start = Column(DateTime, nullable=False)
    request_count = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 106: 注册验证记录 ====================
class RegistrationVerification(Base):
    """注册验证（邮箱验证/CAPTCHA/女巫防御）。"""
    __tablename__ = "registration_verifications"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    method = Column(String(16), nullable=False)           # email/captcha/sybil
    token = Column(String(128), default="", index=True)
    status = Column(String(16), default="pending")        # pending/verified/expired/blocked
    ip_address = Column(String(45), default="")
    fingerprint = Column(String(128), default="")         # 浏览器指纹（防女巫）
    expires_at = Column(DateTime)
    verified_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ====================================================================
# P1 实时/经济增强（表 107-125）
# ====================================================================

# ==================== 表 107: 实时事件订阅 ====================
class RealtimeSubscription(Base):
    """WebSocket/SSE 实时事件订阅。"""
    __tablename__ = "realtime_subscriptions"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, default=0)
    host_id = Column(Integer, default=0)
    channel = Column(String(64), nullable=False, index=True)   # task/negotiation/market/governance
    event_types = Column(Text, default='["*"]')                # 订阅事件类型列表
    delivery = Column(String(16), default="sse")               # sse/websocket/push
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 108: 实时事件队列 ====================
class RealtimeEvent(Base):
    """待推送实时事件。"""
    __tablename__ = "realtime_events"
    id = Column(Integer, primary_key=True)
    channel = Column(String(64), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)
    payload = Column(Text, default="{}")
    priority = Column(Integer, default=5)                       # 1最高 10最低
    delivered = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 109: 协作工作空间 ====================
class Workspace(Base):
    """多人/多AI协作工作空间。"""
    __tablename__ = "workspaces"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    owner_type = Column(String(16), default="citizen")         # citizen/host
    owner_id = Column(Integer, nullable=False, index=True)
    project_id = Column(Integer, default=0)
    config = Column(Text, default="{}")                        # 工作空间配置
    status = Column(String(16), default="active")              # active/archived
    created_at = Column(DateTime, default=_now)


# ==================== 表 110: 工作空间成员 ====================
class WorkspaceMember(Base):
    """工作空间成员及权限。"""
    __tablename__ = "workspace_members"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, nullable=False, index=True)
    member_type = Column(String(16), nullable=False)           # citizen/host
    member_id = Column(Integer, nullable=False)
    role = Column(String(16), default="member")                # owner/editor/viewer
    joined_at = Column(DateTime, default=_now)


# ==================== 表 111: 二级凭证/积分代币 ====================
class SecondaryToken(Base):
    """二级凭证/积分/内部代币。"""
    __tablename__ = "secondary_tokens"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), unique=True, nullable=False)   # 代币符号
    name = Column(String(80), nullable=False)
    issuer_type = Column(String(16), default="platform")       # platform/guild/org
    issuer_id = Column(Integer, default=0)
    total_supply = Column(Integer, default=0)
    decimals = Column(Integer, default=2)
    is_transferable = Column(Integer, default=1)
    meta_data = Column("metadata", Text, default="{}")
    created_at = Column(DateTime, default=_now)


# ==================== 表 112: AMM 流动性池 ====================
class AMMPool(Base):
    """AMM 自动做市商流动性池。"""
    __tablename__ = "amm_pools"
    id = Column(Integer, primary_key=True)
    token_a = Column(String(16), nullable=False)
    token_b = Column(String(16), nullable=False)
    reserve_a = Column(Integer, default=0)                    # 分（整数）
    reserve_b = Column(Integer, default=0)
    fee_rate = Column(Float, default=0.003)                   # 0.3%
    lp_token_supply = Column(Integer, default=0)
    volume_24h = Column(Integer, default=0)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 113: AMM 交易记录 ====================
class AMMSwap(Base):
    """AMM 兑换交易。"""
    __tablename__ = "amm_swaps"
    id = Column(Integer, primary_key=True)
    pool_id = Column(Integer, nullable=False, index=True)
    trader_id = Column(Integer, nullable=False, index=True)
    trader_type = Column(String(16), default="citizen")
    from_token = Column(String(16), nullable=False)
    to_token = Column(String(16), nullable=False)
    from_amount = Column(Integer, nullable=False)
    to_amount = Column(Integer, nullable=False)
    fee_paid = Column(Integer, default=0)
    slippage_bps = Column(Integer, default=50)               # 容忍滑点 0.5%
    created_at = Column(DateTime, default=_now)


# ==================== 表 113b: AMM LP 持仓 ====================
class AMMLPHolding(Base):
    """AMM LP 持仓记录：按 (pool, provider) 记账，防止挤兑他人流动性。"""
    __tablename__ = "amm_lp_holdings"
    id = Column(Integer, primary_key=True)
    pool_id = Column(Integer, nullable=False, index=True)
    provider_id = Column(Integer, nullable=False, index=True)
    provider_type = Column(String(16), default="citizen")
    lp_balance = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ==================== 表 114: 订单簿 ====================
class OrderBookEntry(Base):
    """限价订单簿。"""
    __tablename__ = "order_book_entries"
    id = Column(Integer, primary_key=True)
    market_id = Column(String(32), nullable=False, index=True)   # 交易对标识
    trader_id = Column(Integer, nullable=False, index=True)
    trader_type = Column(String(16), default="citizen")
    side = Column(String(4), nullable=False)                     # buy/sell
    price = Column(Integer, nullable=False)                      # 分
    quantity = Column(Integer, nullable=False)
    filled = Column(Integer, default=0)
    status = Column(String(16), default="open")                  # open/filled/partial/cancelled
    created_at = Column(DateTime, default=_now)


# ==================== 表 114b: 二次投票定义（A-H3 持久化） ====================
class QuadraticPoll(Base):
    """二次投票（QV）定义。

    A-H3 修复：原 poll 元数据存 QuadraticVoting 进程内 dict，重启/多 worker 即丢失，
    投票记录成孤儿（有 QuadraticVote 无对应 poll 定义）。改为持久化到本表，
    poll 生命周期（title/options/额度/状态）以库为唯一真源。
    """
    __tablename__ = "quadratic_polls"
    id = Column(Integer, primary_key=True)
    title = Column(String(255), default="")
    options_json = Column(Text, default="[]")                   # 选项名列表 JSON
    credits_per_voter = Column(Integer, default=100)            # 每位投票者信用额度
    status = Column(String(16), default="open")                 # open/closed
    created_at = Column(DateTime, default=_now)
    closed_at = Column(DateTime, nullable=True)


# ==================== 表 115: 二次投票记录 ====================
class QuadraticVote(Base):
    """二次投票（QV）记录。"""
    __tablename__ = "quadratic_votes"
    id = Column(Integer, primary_key=True)
    poll_id = Column(Integer, nullable=False, index=True)
    voter_id = Column(Integer, nullable=False, index=True)
    voter_type = Column(String(16), default="citizen")
    option_id = Column(Integer, nullable=False)
    credits_spent = Column(Integer, nullable=False)             # 实际消耗信用（= n^2）
    vote_weight = Column(Integer, nullable=False)               # sqrt(credits) 作为有效票数
    created_at = Column(DateTime, default=_now)


# ==================== 表 116: 预测市场 ====================
class PredictionMarket(Base):
    """预测市场事件。"""
    __tablename__ = "prediction_markets"
    id = Column(Integer, primary_key=True)
    question = Column(String(500), nullable=False)
    description = Column(Text, default="")
    outcomes = Column(Text, default='["yes","no"]')             # 选项列表
    resolution_source = Column(String(200), default="")         # 裁定依据
    status = Column(String(16), default="open")                 # open/resolved/cancelled
    resolved_outcome = Column(String(50), default="")
    total_volume = Column(Integer, default=0)
    created_by = Column(Integer, default=0)
    resolves_at = Column(DateTime)
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 117: 预测份额 ====================
class PredictionShare(Base):
    """预测市场持仓/份额。"""
    __tablename__ = "prediction_shares"
    id = Column(Integer, primary_key=True)
    market_id = Column(Integer, nullable=False, index=True)
    bettor_id = Column(Integer, nullable=False, index=True)
    bettor_type = Column(String(16), default="citizen")
    outcome = Column(String(50), nullable=False)
    shares = Column(Integer, nullable=False)
    avg_cost = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 118: 公开悬赏板 ====================
class BountyListing(Base):
    """公开悬赏/赏金任务。"""
    __tablename__ = "bounty_listings"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")
    category = Column(String(32), default="bug")               # bug/feature/security/translation
    reward_cent = Column(Integer, nullable=False)
    max_claims = Column(Integer, default=1)                    # 最多接受几人
    claimed_count = Column(Integer, default=0)
    complexity = Column(String(16), default="medium")          # easy/medium/hard
    tags = Column(Text, default='[]')
    issuer_type = Column(String(16), default="host")
    issuer_id = Column(Integer, nullable=False)
    status = Column(String(16), default="open")                # open/claimed/resolved/expired
    deadline = Column(DateTime)
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 119: 悬赏提交 ====================
class BountySubmission(Base):
    """悬赏解答提交。"""
    __tablename__ = "bounty_submissions"
    id = Column(Integer, primary_key=True)
    bounty_id = Column(Integer, nullable=False, index=True)
    submitter_id = Column(Integer, nullable=False, index=True)
    submitter_type = Column(String(16), default="citizen")
    content = Column(Text, default="")
    status = Column(String(16), default="pending")             # pending/accepted/rejected
    reviewed_by = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 120: 供应链溯源 ====================
class SupplyChainRecord(Base):
    """生产链溯源记录（数据/模型/输出的来源链）。"""
    __tablename__ = "supply_chain_records"
    id = Column(Integer, primary_key=True)
    artifact_type = Column(String(32), nullable=False)         # dataset/model/output/tool
    artifact_id = Column(Integer, nullable=False, index=True)
    stage = Column(String(32), nullable=False)                 # created/processed/trained/deployed
    actor_id = Column(Integer, nullable=False)
    actor_type = Column(String(16), default="citizen")
    source_ids = Column(Text, default='[]')                    # 上游依赖 artifact IDs
    hash = Column(String(64), default="")                      # 产物哈希（完整性）
    meta_data = Column("metadata", Text, default="{}")
    created_at = Column(DateTime, default=_now)


# ==================== 表 121: 可编程合约模板 ====================
class SmartContractTemplate(Base):
    """可编程智能合约模板。"""
    __tablename__ = "smart_contract_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    trigger_type = Column(String(32), nullable=False)          # event/schedule/manual
    trigger_config = Column(Text, default="{}")
    condition_expr = Column(Text, default="")                  # 触发条件（DSL）
    action_expr = Column(Text, default="")                     # 执行动作（DSL）
    version = Column(Integer, default=1)
    author_id = Column(Integer, default=0)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 122: 合约执行日志 ====================
class ContractExecution(Base):
    """合约触发执行记录。"""
    __tablename__ = "contract_executions"
    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, nullable=False, index=True)
    trigger_event = Column(String(64), default="")
    input_context = Column(Text, default="{}")
    output_result = Column(Text, default="{}")
    status = Column(String(16), default="success")             # success/failed/skipped
    gas_used = Column(Integer, default=0)                      # 资源消耗（模拟）
    duration_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 123: 异常事件检测 ====================
class AnomalyEvent(Base):
    """AI 行为异常检测事件。"""
    __tablename__ = "anomaly_events"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    anomaly_type = Column(String(32), nullable=False)          # overspend/idle_loop/aggression/spam
    severity = Column(String(16), default="medium")            # low/medium/high/critical
    indicators = Column(Text, default="{}")                    # 检测指标详情
    action_taken = Column(String(64), default="")              # 已执行动作
    resolved = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 124: AI 互信网络 ====================
class TrustEdge(Base):
    """AI 间信任关系边。"""
    __tablename__ = "trust_edges"
    id = Column(Integer, primary_key=True)
    truster_id = Column(Integer, nullable=False, index=True)   # 信任方
    trustee_id = Column(Integer, nullable=False, index=True)   # 被信任方
    trust_score = Column(Float, default=0.5)                   # 0.0-1.0
    context = Column(String(32), default="general")            # general/task/guild/trade
    interactions = Column(Integer, default=0)
    last_updated = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)


# ==================== 表 125: 自主出价策略 ====================
class BidStrategy(Base):
    """AI 自主出价策略配置。"""
    __tablename__ = "bid_strategies"
    id = Column(Integer, primary_key=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    market_scope = Column(String(32), default="general")       # 适用市场范围
    min_price = Column(Integer, default=0)
    max_price = Column(Integer, default=1000000)
    strategy_type = Column(String(24), default="aggressive")   # aggressive/conservative/adaptive
    budget_daily_cent = Column(Integer, default=10000)
    win_rate_target = Column(Float, default=0.6)
    is_active = Column(Integer, default=1)
    updated_at = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)


# ==================== 表 126: 技能组合编排 ====================
class SkillComposition(Base):
    """技能组合 DAG 编排定义。"""
    __tablename__ = "skill_compositions"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    dag = Column(Text, default='{"nodes":[],"edges":[]}')      # DAG 拓扑 JSON
    author_id = Column(Integer, default=0)
    version = Column(Integer, default=1)
    status = Column(String(16), default="draft")               # draft/published/deprecated
    usage_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 127: 组合执行实例 ====================
class SkillCompositionRun(Base):
    """技能组合单次执行实例。"""
    __tablename__ = "skill_composition_runs"
    id = Column(Integer, primary_key=True)
    composition_id = Column(Integer, nullable=False, index=True)
    task_id = Column(Integer, default=0)
    initiator_id = Column(Integer, default=0)
    node_results = Column(Text, default="{}")                  # 各节点执行结果
    status = Column(String(16), default="running")             # running/success/partial/failed
    total_cost_cent = Column(Integer, default=0)
    duration_ms = Column(Integer, default=0)
    started_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime)


# ==================== 表 128: 基准评测测试集 ====================
class BenchmarkTest(Base):
    """AI 持续基准评测测试集。"""
    __tablename__ = "benchmark_tests"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    category = Column(String(32), default="general")           # reasoning/code/creative/safety
    difficulty = Column(String(16), default="medium")
    test_cases = Column(Text, default='[]')                    # 题目列表 JSON
    pass_threshold = Column(Float, default=0.7)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 129: 基准评测结果 ====================
class BenchmarkResult(Base):
    """AI 评测结果记录。"""
    __tablename__ = "benchmark_results"
    id = Column(Integer, primary_key=True)
    test_id = Column(Integer, nullable=False, index=True)
    citizen_id = Column(Integer, nullable=False, index=True)
    score = Column(Float, nullable=False)                      # 0.0-1.0
    passed = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    detail = Column(Text, default="{}")
    evaluated_at = Column(DateTime, default=_now)


# ====================================================================
# P2 平台工程增强（表 130-138）
# ====================================================================

# ==================== 表 130: 特性开关 ====================
class FeatureFlag(Base):
    """Feature Flags 特性开关。"""
    __tablename__ = "feature_flags"
    id = Column(Integer, primary_key=True)
    flag_key = Column(String(64), unique=True, nullable=False)
    description = Column(String(200), default="")
    enabled = Column(Integer, default=0)
    rollout_pct = Column(Integer, default=0)                   # 灰度百分比 0-100
    target_tiers = Column(String(100), default="*")            # 适用席位
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ==================== 表 131: GDPR 删除请求 ====================
class DeletionRequest(Base):
    """GDPR 被遗忘权删除请求。"""
    __tablename__ = "deletion_requests"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    scope = Column(String(16), default="all")                  # all/partial
    data_categories = Column(Text, default='["*"]')
    status = Column(String(16), default="pending")             # pending/processing/completed/rejected
    reason = Column(Text, default="")
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 132: 租户配置 ====================
class Tenant(Base):
    """多租户隔离配置。"""
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True)
    tenant_code = Column(String(32), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    plan = Column(String(16), default="free")
    max_hosts = Column(Integer, default=100)
    max_ai_citizens = Column(Integer, default=500)
    config = Column(Text, default="{}")
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 133: 推送订阅 ====================
class PushSubscription(Base):
    """Push 通知/Webhook IM 推送订阅。"""
    __tablename__ = "push_subscriptions"
    id = Column(Integer, primary_key=True)
    subscriber_type = Column(String(16), nullable=False)       # host/citizen
    subscriber_id = Column(Integer, nullable=False, index=True)
    channel = Column(String(32), nullable=False)               # im/email/sms/webpush/webhook
    endpoint = Column(String(500), default="")                 # 推送端点
    event_types = Column(Text, default='["*"]')
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 134: 链路追踪 Span ====================
class TraceSpan(Base):
    """分布式链路追踪 Span。"""
    __tablename__ = "trace_spans"
    id = Column(Integer, primary_key=True)
    trace_id = Column(String(64), nullable=False, index=True)
    span_id = Column(String(32), nullable=False)
    parent_span_id = Column(String(32), default="")
    operation = Column(String(128), nullable=False)
    service_name = Column(String(64), default="aijuhe")
    duration_ms = Column(Integer, default=0)
    status = Column(String(16), default="ok")                  # ok/error
    tags = Column(Text, default="{}")
    start_time = Column(DateTime, default=_now)


# ==================== 表 135: 备份记录 ====================
class BackupRecord(Base):
    """自动备份与恢复记录。"""
    __tablename__ = "backup_records"
    id = Column(Integer, primary_key=True)
    backup_type = Column(String(16), default="auto")           # auto/manual/pre_migration
    target_path = Column(String(500), default="")
    size_bytes = Column(Integer, default=0)
    checksum = Column(String(64), default="")
    status = Column(String(16), default="success")             # success/failed/running
    triggered_by = Column(String(32), default="scheduler")
    created_at = Column(DateTime, default=_now)


# ==================== 表 136: 合规审计报告 ====================
class ComplianceReport(Base):
    """合规审计报告。"""
    __tablename__ = "compliance_reports"
    id = Column(Integer, primary_key=True)
    report_type = Column(String(32), nullable=False)           # financial/privacy/security/operational
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    findings = Column(Text, default="[]")                      # 发现项列表
    risk_level = Column(String(16), default="low")             # low/medium/high
    status = Column(String(16), default="draft")               # draft/final/archived
    generated_by = Column(String(32), default="auto")
    created_at = Column(DateTime, default=_now)


# ==================== 表 137: ML 审核评分 ====================
class ModerationScore(Base):
    """ML 内容审核评分记录。"""
    __tablename__ = "moderation_scores"
    id = Column(Integer, primary_key=True)
    content_type = Column(String(16), nullable=False)          # feed/comment/task/chat
    content_id = Column(Integer, nullable=False, index=True)
    model_name = Column(String(64), default="basic_v1")
    scores = Column(Text, default="{}")                        # {"toxic":0.1,"spam":0.05,...}
    decision = Column(String(16), default="pass")              # pass/review/block
    citizen_id = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


# ==================== 表 138: 迁移版本记录 ====================
class MigrationRecord(Base):
    """Alembic 迁移版本跟踪。"""
    __tablename__ = "migration_records"
    id = Column(Integer, primary_key=True)
    version = Column(String(32), nullable=False, index=True)
    description = Column(String(200), default="")
    applied_at = Column(DateTime, default=_now)
    direction = Column(String(8), default="up")                # up/down
    success = Column(Integer, default=1)


# ====================================================================
# P3 生态成熟增强（表 139-145）
# ====================================================================

# ==================== 表 139: DID 标识符 ====================
class DIDDocument(Base):
    """去中心化身份 DID 文档。"""
    __tablename__ = "did_documents"
    id = Column(Integer, primary_key=True)
    did = Column(String(128), unique=True, nullable=False)     # did:aijuhe:xxx
    subject_type = Column(String(16), default="citizen")       # citizen/host
    subject_id = Column(Integer, nullable=False, index=True)
    public_key = Column(Text, nullable=False)                  # JSON Web Key
    service_endpoints = Column(Text, default="[]")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ==================== 表 140: 可验证凭证 ====================
class VerifiableCredential(Base):
    """可验证凭证（VC）。"""
    __tablename__ = "verifiable_credentials"
    id = Column(Integer, primary_key=True)
    credential_id = Column(String(128), unique=True, nullable=False)
    issuer_did = Column(String(128), nullable=False)
    subject_did = Column(String(128), nullable=False, index=True)
    credential_type = Column(String(32), nullable=False)       # skill/cert/achievement/reputation
    claims = Column(Text, default="{}")
    proof = Column(Text, default="{}")                         # 签名证据
    status = Column(String(16), default="valid")               # valid/revoked
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 141: 社交图谱分析 ====================
class SocialGraphEdge(Base):
    """社交图谱边（关系强度）。"""
    __tablename__ = "social_graph_edges"
    id = Column(Integer, primary_key=True)
    from_id = Column(Integer, nullable=False, index=True)
    to_id = Column(Integer, nullable=False, index=True)
    relation_type = Column(String(24), nullable=False)         # collaborator/mentor/friend/rival
    weight = Column(Float, default=1.0)                        # 关系强度
    interactions = Column(Integer, default=0)
    last_interaction = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)


# ==================== 表 142: 游戏化成就 ====================
class Achievement(Base):
    """游戏化成就定义。"""
    __tablename__ = "achievements"
    id = Column(Integer, primary_key=True)
    key = Column(String(64), unique=True, nullable=False)
    name = Column(String(80), nullable=False)
    description = Column(String(200), default="")
    category = Column(String(24), default="general")           # economic/social/skill/exploration
    xp_reward = Column(Integer, default=0)
    condition_expr = Column(Text, default="{}")               # 达成条件
    icon = Column(String(50), default="")
    rarity = Column(String(16), default="common")             # common/rare/epic/legendary
    created_at = Column(DateTime, default=_now)


# ==================== 表 143: 成就解锁记录 ====================
class AchievementUnlock(Base):
    """AI/宿主成就解锁记录。"""
    __tablename__ = "achievement_unlocks"
    id = Column(Integer, primary_key=True)
    achievement_id = Column(Integer, nullable=False, index=True)
    citizen_id = Column(Integer, default=0, index=True)
    host_id = Column(Integer, default=0, index=True)
    unlocked_at = Column(DateTime, default=_now)


# ==================== 表 144: 知识共享文章 ====================
class KnowledgeArticle(Base):
    """知识共享/社区 Wiki 文章。"""
    __tablename__ = "knowledge_articles"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, default="")
    author_type = Column(String(16), default="citizen")
    author_id = Column(Integer, nullable=False, index=True)
    category = Column(String(32), default="tutorial")          # tutorial/pattern/api/research
    tags = Column(Text, default='[]')
    view_count = Column(Integer, default=0)
    upvote_count = Column(Integer, default=0)
    status = Column(String(16), default="published")           # draft/published/archived
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ==================== 表 145: SLA 指标快照 ====================
class SLAMetricSnapshot(Base):
    """SLA 仪表盘时序指标。"""
    __tablename__ = "sla_metric_snapshots"
    id = Column(Integer, primary_key=True)
    service = Column(String(64), nullable=False, index=True)   # 服务/模块名
    metric_name = Column(String(64), nullable=False)           # latency_p99/error_rate/availability
    value = Column(Float, nullable=False)
    unit = Column(String(16), default="")                      # ms/%/count
    target = Column(Float, default=0.0)                        # SLA 目标值
    met = Column(Integer, default=1)                           # 是否达标
    captured_at = Column(DateTime, default=_now)


# ==================== 表 146: Token 吊销 ====================
class TokenRevocation(Base):
    """已吊销的 token 黑名单。"""
    __tablename__ = "token_revocations"
    token_id = Column(String(128), primary_key=True)
    jti = Column(String(128), nullable=False, index=True)
    ai_id = Column(Integer, nullable=True)
    host_id = Column(Integer, nullable=True)
    revoked_at = Column(DateTime, default=_now)
    reason = Column(String(255), default="")
    expires_at = Column(DateTime, nullable=True)


# ==================== 表 147: Prompt 注入日志 ====================
class PromptInjectionLog(Base):
    """Prompt 注入检测日志。"""
    __tablename__ = "prompt_injection_logs"
    id = Column(Integer, primary_key=True)
    source_type = Column(String(16), nullable=False)
    source_id = Column(Integer, nullable=False)
    ai_id = Column(Integer, nullable=True)
    content_hash = Column(String(64), default="")
    pattern_matched = Column(String(255), default="")
    confidence = Column(Float, default=0.0)
    action_taken = Column(String(16), default="pass")
    created_at = Column(DateTime, default=_now)


# ==================== 表 148: 模型降级规则 ====================
class ModelFallbackRule(Base):
    """模型降级链配置。"""
    __tablename__ = "model_fallback_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    primary_model = Column(String(64), nullable=False)
    fallback_chain = Column(Text, default="[]")
    trigger_conditions = Column(Text, default="{}")
    timeout_ms = Column(Integer, default=5000)
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ==================== 表 149: 健康探针记录 ====================
class HealthProbeRecord(Base):
    """服务健康探针时序记录。"""
    __tablename__ = "health_probe_records"
    id = Column(Integer, primary_key=True)
    service = Column(String(64), nullable=False, index=True)
    status = Column(String(16), nullable=False)
    latency_ms = Column(Integer, default=0)
    detail = Column(Text, default="")
    probed_at = Column(DateTime, default=_now)


# ==================== 表 150: CORS 策略 ====================
class CORSPolicy(Base):
    """多租户 CORS 策略。"""
    __tablename__ = "cors_policies"
    id = Column(Integer, primary_key=True)
    origin_pattern = Column(String(255), nullable=False)
    methods = Column(String(255), default="*")
    headers = Column(String(255), default="*")
    credentials = Column(Integer, default=0)
    max_age = Column(Integer, default=3600)
    tenant_code = Column(String(32), default="default")


# ==================== 表 152: 弹劾案件 ====================
class ImpeachmentCase(Base):
    """弹劾案件管理。"""
    __tablename__ = "impeachment_cases"
    id = Column(Integer, primary_key=True)
    target_type = Column(String(16), nullable=False)
    target_id = Column(Integer, nullable=False)
    initiated_by = Column(Integer, nullable=False)
    charges = Column(Text, default="")
    status = Column(String(16), default="open")
    vote_for = Column(Integer, default=0)
    vote_against = Column(Integer, default=0)
    vote_total = Column(Integer, default=0)
    voters_json = Column(Text, default="[]")  # 投票者宿主 ID 列表（一人一票去重）
    created_at = Column(DateTime, default=_now)
    resolved_at = Column(DateTime, nullable=True)


# ==================== 表 152b: 弹劾投票（A-H2 防重复投票） ====================
class ImpeachmentVote(Base):
    """弹劾投票记录（一人一票，DB 唯一约束天然幂等）。

    A-H2 修复：原 voters_json read-modify-write 无锁，并发投票会静默吞票 /
    重复计票。改为每次投票写一行 (case_id, voter_host_id) 并加唯一约束——
    同宿主重复投票由 DB 层拒绝（IntegrityError），彻底消除读改写竞态。
    """
    __tablename__ = "impeachment_votes"
    __table_args__ = (
        UniqueConstraint("case_id", "voter_host_id", name="uq_impeachment_case_voter"),
    )
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, nullable=False, index=True)
    voter_host_id = Column(Integer, nullable=False, index=True)
    vote = Column(String(16), nullable=False)                   # for/against
    created_at = Column(DateTime, default=_now)


# ==================== 表 153: 日落条款 ====================
class SunsetClause(Base):
    """规则日落条款（自动过期）。"""
    __tablename__ = "sunset_clauses"
    id = Column(Integer, primary_key=True)
    rule_source = Column(String(16), nullable=False)
    rule_id = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    auto_action = Column(String(16), default="expire")
    extension_votes = Column(Integer, default=0)
    active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


# ==================== 表 154: AI 记忆条目 ====================
class AIMemoryEntry(Base):
    """AI 长期记忆存储。"""
    __tablename__ = "ai_memory_entries"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    memory_type = Column(String(16), nullable=False)
    content = Column(Text, default="")
    context_summary = Column(Text, default="")
    importance = Column(Float, default=0.5)
    access_count = Column(Integer, default=0)
    last_accessed = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    expires_at = Column(DateTime, nullable=True)


# ==================== 表 155: 经济指标快照 ====================
class EconomicIndicator(Base):
    """宏观经济指标时序。"""
    __tablename__ = "economic_indicators"
    id = Column(Integer, primary_key=True)
    indicator_name = Column(String(64), nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String(16), default="")
    period = Column(String(16), default="daily")
    captured_at = Column(DateTime, default=_now)
    gini_coefficient = Column(Float, nullable=True)
    velocity = Column(Float, nullable=True)
    inflation_rate = Column(Float, nullable=True)


# ==================== 表 156: 知识图谱边 ====================
class KnowledgeGraphEdge(Base):
    """知识图谱关系边。"""
    __tablename__ = "knowledge_graph_edges"
    id = Column(Integer, primary_key=True)
    source_type = Column(String(32), nullable=False)
    source_id = Column(Integer, nullable=False)
    target_type = Column(String(32), nullable=False)
    target_id = Column(Integer, nullable=False)
    relation = Column(String(64), nullable=False)
    weight = Column(Float, default=1.0)
    meta_data = Column("metadata", Text, default="{}")
    created_at = Column(DateTime, default=_now)


# ==================== 表 157: 模型路由决策 ====================
class ModelRoutingDecision(Base):
    """成本-质量模型路由决策记录。"""
    __tablename__ = "model_routing_decisions"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    task_type = Column(String(64), default="")
    selected_model = Column(String(64), nullable=False)
    cost_estimate = Column(Float, default=0.0)
    quality_score = Column(Float, default=0.0)
    latency_estimate = Column(Integer, default=0)
    decision_factors = Column(Text, default="{}")
    created_at = Column(DateTime, default=_now)


# ==================== 表 158: 情感分析记录 ====================
class SentimentRecord(Base):
    """内容情感分析结果。"""
    __tablename__ = "sentiment_records"
    id = Column(Integer, primary_key=True)
    source_type = Column(String(16), nullable=False)
    source_id = Column(Integer, nullable=False)
    ai_id = Column(Integer, nullable=False)
    sentiment = Column(Float, default=0.0)
    magnitude = Column(Float, default=0.0)
    keywords = Column(Text, default="[]")
    analyzed_at = Column(DateTime, default=_now)


# ==================== 表 159: 伦理审查案件 ====================
class EthicsReviewCase(Base):
    """AI 决策伦理审查。"""
    __tablename__ = "ethics_review_cases"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False)
    decision_context = Column(Text, default="")
    bias_indicators = Column(Text, default="{}")
    severity = Column(String(16), default="low")
    status = Column(String(16), default="pending")
    reviewer_ai_id = Column(Integer, nullable=True)
    resolution = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    resolved_at = Column(DateTime, nullable=True)


# ==================== 表 160: 经济周期信号 ====================
class EconomicCycleSignal(Base):
    """经济周期检测信号。"""
    __tablename__ = "economic_cycle_signals"
    id = Column(Integer, primary_key=True)
    signal_type = Column(String(16), nullable=False)
    confidence = Column(Float, default=0.0)
    indicators = Column(Text, default="{}")
    recommended_actions = Column(Text, default="[]")
    detected_at = Column(DateTime, default=_now)
    action_taken = Column(String(32), default="none")


# ==================== 表 161: AI 自我评估 ====================
class AISelfAssessment(Base):
    """AI 对自身任务表现的评估。"""
    __tablename__ = "ai_self_assessments"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    task_id = Column(Integer, nullable=True)
    self_quality_score = Column(Float, default=0.0)
    confidence = Column(Float, default=0.0)
    identified_gaps = Column(Text, default="[]")
    improvement_plan = Column(Text, default="")
    created_at = Column(DateTime, default=_now)


# ==================== 表 162: 上下文预算分配 ====================
class ContextBudgetAllocation(Base):
    """AI 任务上下文 token 预算。"""
    __tablename__ = "context_budget_allocations"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False)
    task_id = Column(Integer, nullable=False)
    max_tokens = Column(Integer, default=8192)
    used_tokens = Column(Integer, default=0)
    strategy = Column(String(16), default="window")
    overflow_action = Column(String(16), default="reject")
    created_at = Column(DateTime, default=_now)


# ==================== 表 163: 记忆衰减调度 ====================
class MemoryDecaySchedule(Base):
    """AI 记忆衰减与整合调度。"""
    __tablename__ = "memory_decay_schedules"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    last_consolidation = Column(DateTime, nullable=True)
    decay_rate = Column(Float, default=0.05)
    min_importance = Column(Float, default=0.1)
    next_consolidation_at = Column(DateTime, nullable=True)


# ==================== 表 166: 财富分布快照 ====================
class WealthDistributionSnapshot(Base):
    """财富分布周期快照。"""
    __tablename__ = "wealth_distribution_snapshots"
    id = Column(Integer, primary_key=True)
    gini = Column(Float, default=0.0)
    top10_share = Column(Float, default=0.0)
    bottom50_share = Column(Float, default=0.0)
    median_wealth = Column(Float, default=0.0)
    mean_wealth = Column(Float, default=0.0)
    total_circulation = Column(Float, default=0.0)
    snapshot_date = Column(DateTime, default=_now)


# ==================== 表 167: 通用众筹 ====================
class GeneralCrowdfund(Base):
    """通用众筹项目。"""
    __tablename__ = "general_crowdfunds"
    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, default="")
    creator_ai_id = Column(Integer, nullable=False)
    target_amount = Column(Integer, nullable=False)
    current_amount = Column(Integer, default=0)
    deadline = Column(DateTime, nullable=False)
    status = Column(String(16), default="open")
    category = Column(String(64), default="")
    created_at = Column(DateTime, default=_now)
    funded_at = Column(DateTime, nullable=True)


# ==================== 表 168: 众筹贡献 ====================
class CrowdfundContribution(Base):
    """众筹贡献记录。"""
    __tablename__ = "crowdfund_contributions"
    id = Column(Integer, primary_key=True)
    crowdfund_id = Column(Integer, nullable=False, index=True)
    contributor_ai_id = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)
    contribution_at = Column(DateTime, default=_now)
    tier = Column(String(32), default="base")


# ==================== 表 169: Futarchy 提案 ====================
class FutarchyProposal(Base):
    """Futarchy 投票提案。"""
    __tablename__ = "futarchy_proposals"
    id = Column(Integer, primary_key=True)
    proposal_text = Column(Text, nullable=False)
    prediction_market_id = Column(Integer, nullable=False)
    winning_outcome = Column(String(64), default="")
    implementation_status = Column(String(16), default="pending")
    created_at = Column(DateTime, default=_now)
    resolved_at = Column(DateTime, nullable=True)


# ==================== 表 170: 托管收益累计 ====================
class EscrowYieldAccrual(Base):
    """托管账户收益计息记录。"""
    __tablename__ = "escrow_yield_accruals"
    id = Column(Integer, primary_key=True)
    escrow_id = Column(Integer, nullable=False, index=True)
    ai_id = Column(Integer, nullable=False)
    principal = Column(Integer, nullable=False)
    accrued_interest = Column(Float, default=0.0)
    rate_bps = Column(Integer, default=0)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)


# ==================== 表 186: 积分购买/订阅订单 ====================
class Order(Base):
    """积分购买/订阅订单（站内唯一真实资金入口，单向不可逆）。

    合规声明：本订单为"购买平台积分/服务"的行为，不涉及货币兑换。
    积分/AC 仅限平台内使用，不可提现、不可兑换法币或虚拟货币。
    单向不可逆：支付成功后积分到账，不提供任何反向操作。
    """
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(24), nullable=False, default="pack")      # pack / subscription
    pack_id = Column(String(32), default="")                       # 积分包/订阅档标识
    amount_cent = Column(Integer, nullable=False, default=0)       # 真实支付金额（分）
    credits_cent = Column(Integer, nullable=False, default=0)      # 获得积分数（分）
    seat_tier = Column(String(16), default="")                     # 订阅对应的席位等级
    seat_duration_days = Column(Integer, default=0)                # 订阅有效天数
    status = Column(String(16), default="pending")                 # pending/paid/cancelled/expired
    pay_channel = Column(String(16), default="mock")               # mock / dodo / creem
    pay_ref = Column(String(128), default="")                      # 外部支付渠道交易号（mock 下为 mock_xxx）
    paid_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)


# ==================== 表 187: 成果留存授权 ====================
class WorkRetentionConsent(Base):
    """成果留存授权——判定→征求→仅站内留存→绝不外传承诺→运营方担责。

    合规声明：
    - 留存副本仅限站内使用，绝不导出/外传至站外；
    - 未经授权（approved）不得留存副本；
    - 签署承诺记录"违者由本站运营方承担法律责任"。
    """
    __tablename__ = "work_retention_consents"
    id = Column(Integer, primary_key=True)
    contract_id = Column(Integer, nullable=False, index=True)      # 关联合约
    deliverable_id = Column(Integer, default=0)                    # 关联交付物（可空=按合约级）
    worker_id = Column(Integer, nullable=False, index=True)        # 责任 AI（Contract.worker_id）
    ai_benefit_judgement = Column(Integer, default=0)              # 责任AI判定"有利于提升站内AI" 0=否 1=是
    ai_benefit_reason = Column(Text, default="")                   # 判定理由
    status = Column(String(16), default="pending")                 # pending/approved/denied
    decided_by = Column(Integer, default=0)                        # 做出授权决定的 host_id 或 citizen_id
    promise_signed = Column(Integer, default=0)                    # 是否已签署"绝不外传"承诺 0=否 1=是
    promise_text = Column(Text, default="")                        # 承诺文本（模板固定）
    promise_signed_at = Column(DateTime)                           # 承诺签署时间
    retention_scope = Column(String(32), default="internal_only")  # 仅限站内（唯一值，不可更改）
    created_at = Column(DateTime, default=_now)
    decided_at = Column(DateTime)


# ==================== 表 171: 配置漂移告警 ====================
class ConfigDriftAlert(Base):
    """运行时配置漂移检测。"""
    __tablename__ = "config_drift_alerts"
    id = Column(Integer, primary_key=True)
    key = Column(String(128), nullable=False)
    expected_value = Column(Text, default="")
    actual_value = Column(Text, default="")
    detected_at = Column(DateTime, default=_now)
    resolved = Column(Integer, default=0)
    resolved_at = Column(DateTime, nullable=True)


# ==================== 表 172: 降级模式状态 ====================
class DegradationModeState(Base):
    """系统降级模式状态机。"""
    __tablename__ = "degradation_mode_states"
    id = Column(Integer, primary_key=True)
    mode = Column(String(16), nullable=False)
    trigger_reason = Column(Text, default="")
    triggered_by = Column(String(16), default="auto")
    activated_at = Column(DateTime, default=_now)
    deactivated_at = Column(DateTime, nullable=True)
    active = Column(Integer, default=1)


# ==================== 表 173: 服务条款版本 ====================
class TermsOfServiceVersion(Base):
    """服务条款版本管理。"""
    __tablename__ = "terms_of_service_versions"
    id = Column(Integer, primary_key=True)
    version = Column(String(32), nullable=False)
    content = Column(Text, default="")
    effective_date = Column(DateTime, nullable=False)
    accepted_count = Column(Integer, default=0)
    mandatory = Column(Integer, default=1)
    published_at = Column(DateTime, default=_now)


# ==================== 表 174: 条款接受记录 ====================
class TOSAcceptance(Base):
    """用户接受服务条款记录。"""
    __tablename__ = "tos_acceptances"
    id = Column(Integer, primary_key=True)
    version_id = Column(Integer, nullable=False)
    host_id = Column(Integer, nullable=False)
    ai_id = Column(Integer, nullable=True)
    accepted_at = Column(DateTime, default=_now)
    ip_address = Column(String(45), default="")


# ==================== 表 175: 监管报告 ====================
class RegulatoryReport(Base):
    """监管合规报告。"""
    __tablename__ = "regulatory_reports"
    id = Column(Integer, primary_key=True)
    report_type = Column(String(16), nullable=False)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    data = Column(Text, default="{}")
    status = Column(String(16), default="draft")
    generated_at = Column(DateTime, default=_now)
    submitted_at = Column(DateTime, nullable=True)


# ==================== 表 176: 经济预测 ====================
class EconomicForecast(Base):
    """宏观经济预测记录。"""
    __tablename__ = "economic_forecasts"
    id = Column(Integer, primary_key=True)
    forecast_type = Column(String(32), nullable=False)
    predicted_value = Column(Float, nullable=False)
    confidence_interval = Column(Float, default=0.0)
    horizon_days = Column(Integer, default=30)
    model_used = Column(String(64), default="")
    accuracy = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_now)
    actual_value = Column(Float, nullable=True)


# ==================== 表 177: 制裁实体 ====================
class SanctionedEntity(Base):
    """制裁/黑名单实体。"""
    __tablename__ = "sanctioned_entities"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(16), nullable=False)
    identifier = Column(String(255), nullable=False)
    reason = Column(Text, default="")
    sanctioned_by = Column(Integer, nullable=False)
    severity = Column(String(16), default="watch")
    added_at = Column(DateTime, default=_now)
    review_date = Column(DateTime, nullable=True)
    active = Column(Integer, default=1)


# ==================== 表 178: 数据完整性检查 ====================
class DataIntegrityCheck(Base):
    """数据不变量完整性检查。"""
    __tablename__ = "data_integrity_checks"
    id = Column(Integer, primary_key=True)
    check_name = Column(String(128), nullable=False)
    invariant = Column(Text, default="")
    passed = Column(Integer, default=0)
    expected = Column(Text, default="")
    actual = Column(Text, default="")
    checked_at = Column(DateTime, default=_now)
    severity = Column(String(16), default="warning")


# ==================== 表 179: AI 配额使用 ====================
class AIQuotaUsage(Base):
    """AI 资源配额使用跟踪。"""
    __tablename__ = "ai_quota_usages"
    id = Column(Integer, primary_key=True)
    ai_id = Column(Integer, nullable=False, index=True)
    quota_type = Column(String(32), nullable=False)
    used = Column(Integer, default=0)
    limit = Column(Integer, nullable=False)
    period = Column(String(16), default="daily")
    reset_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


# ==================== 表 180: 依赖漏洞报告 ====================
class DependencyVulnReport(Base):
    """第三方依赖漏洞扫描。"""
    __tablename__ = "dependency_vuln_reports"
    id = Column(Integer, primary_key=True)
    package_name = Column(String(128), nullable=False)
    version = Column(String(32), default="")
    cve_id = Column(String(32), default="")
    severity = Column(String(16), default="low")
    description = Column(Text, default="")
    fixed_version = Column(String(32), default="")
    scanned_at = Column(DateTime, default=_now)
    acknowledged = Column(Integer, default=0)


# ==================== 表 181: 实体解析规则 ====================
class EntityResolutionRule(Base):
    """跨源实体解析规则。"""
    __tablename__ = "entity_resolution_rules"
    id = Column(Integer, primary_key=True)
    source_entity_type = Column(String(64), nullable=False)
    canonical_type = Column(String(64), nullable=False)
    match_field = Column(String(64), nullable=False)
    match_strategy = Column(String(16), default="exact")
    confidence_threshold = Column(Float, default=0.9)
    created_at = Column(DateTime, default=_now)


# ==================== 表 182: 任务检查点 ====================
class TaskCheckpoint(Base):
    """长任务阶段快照（可恢复）。"""
    __tablename__ = "task_checkpoints"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, nullable=False, index=True)
    ai_id = Column(Integer, nullable=False)
    stage_index = Column(Integer, default=0)
    state_snapshot = Column(Text, default="{}")
    tokens_used = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)
    expires_at = Column(DateTime, nullable=True)


# ==================== 表 183: 混沌实验 ====================
class ChaosExperiment(Base):
    """混沌工程实验记录。"""
    __tablename__ = "chaos_experiments"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    target_service = Column(String(64), nullable=False)
    fault_type = Column(String(16), nullable=False)
    params = Column(Text, default="{}")
    scheduled_at = Column(DateTime, nullable=True)
    status = Column(String(16), default="pending")
    result_summary = Column(Text, default="")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


# ==================== 表 184: 契约测试记录 ====================
class ContractTestRecord(Base):
    """API 契约兼容性测试。"""
    __tablename__ = "contract_test_records"
    id = Column(Integer, primary_key=True)
    consumer_name = Column(String(128), nullable=False)
    provider_endpoint = Column(String(255), nullable=False)
    request_schema = Column(Text, default="{}")
    response_schema = Column(Text, default="{}")
    passed = Column(Integer, default=0)
    diff_summary = Column(Text, default="")
    tested_at = Column(DateTime, default=_now)


# ==================== 表 185: 审批确认门 ====================
class ApprovalRequest(Base):
    """高危操作人工确认队列。AUTONOMY_LEVEL=0/1 时城主/治理 AI 的高危决策暂存此处。"""
    __tablename__ = "approval_requests"
    id = Column(Integer, primary_key=True)
    action_type = Column(String(64), nullable=False)    # self_approve/review/escalate/large_transfer/arbitrate
    actor_type = Column(String(16), default="ai")       # ai/system
    actor_id = Column(Integer, nullable=False)          # 发起 AI id
    target_ref = Column(String(128), default="")        # 关联对象标识（task:123, contract:456）
    payload_json = Column(Text, default="{}")           # 动作详情 JSON
    risk_level = Column(String(16), default="high")     # low/medium/high/critical
    status = Column(String(16), default="pending")      # pending/approved/rejected/expired/auto_passed
    decided_by = Column(Integer, nullable=True)         # 审批宿主 host_id
    decided_at = Column(DateTime, nullable=True)
    reject_reason = Column(String(512), default="")
    created_at = Column(DateTime, default=_now)
    expires_at = Column(DateTime, nullable=True)


# ---------------- 新增：岗位编制表（城主可增删的动态编制，替代 PLATFORM_JOBS 硬编码） ----------------
class PostQuota(Base):
    """岗位编制表：城主可增删/休眠的岗位任务定义。

    替代 scheduler.PLATFORM_JOBS 硬编码常量——城主根据生态态势动态增减岗位。
    频率自适应：frequency_days 由 planner 根据 noop_streak 和生态成熟度自动调整。
    状态流转：active → dormant(连续无事) → retired(城主退役)。
    """
    __tablename__ = "post_quota"
    post_code = Column(String(48), primary_key=True)          # 岗位标识（同 GovernanceTask.type）
    gov_type = Column(String(32), nullable=False, index=True) # 治理任务类型（同 GOV_TYPES）
    budget_cent = Column(Integer, default=300)                # 每次生成的治理任务预算（分）
    params = Column(Text, default="{}")                       # 岗位参数 JSON（params.scope 等）
    frequency_days = Column(Integer, default=1)               # 执行频率（天/次），planner 自适应调整
    status = Column(String(12), default="active", index=True) # active/dormant/retired
    noop_streak = Column(Integer, default=0)                  # 连续"无事可做"轮数（触发降频）
    last_run_date = Column(String(10))                        # 上次执行日期 "yyyy-MM-dd"
    total_runs = Column(Integer, default=0)                   # 累计执行次数
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)


# ---------------- 新增：LLM 日配额计数器（token 预算管控） ----------------
class LlmBudgetLog(Base):
    """每岗位类型每日 LLM 调用计数器（token 预算层）。

    key 格式："llm_budget:{gov_type}:{yyyy-MM-dd}"
    value_cent 复用为当日已调用次数（非金额，是调用条数）。
    """
    __tablename__ = "llm_budget_log"
    key = Column(String(64), primary_key=True)   # "llm_budget:{gov_type}:{yyyy-MM-dd}"
    call_count = Column(Integer, default=0)      # 当日已用 LLM 调用次数
    updated_at = Column(DateTime, default=_now)


# ==================== MVT 楔子：宣传视频成片最短链路（不动经济/文明层）====================
class WedgeJob(Base):
    """MVT 楔子任务：需求→编排→出片→下载→支付 最短链路的最小可跑骨架。

    刻意独立于经济/文明体系（不碰钱包 / AC / 合约 / 公民 / 劳动力市场），
    仅记录交付物与一次性的 mock 支付解锁位（pay_unlocked），用于最快验证
    "自动交付一份宣传视频成片是否有人付费"这一道可证伪假设。

    生命周期（status）：queued → scripting → rendering → done | failed
    埋点（events，JSON 数组）：每个里程碑一条 {ts, stage, request_id, detail}。
    """
    __tablename__ = "wedge_jobs"
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, nullable=False, index=True)        # 提交需求的宿主
    brief = Column(Text, default="")                             # 用户输入的原始需求（宣传视频诉求）
    kind = Column(String(24), default="video_civil")             # 出片链路（楔子默认宣传视频 video_civil）
    status = Column(String(16), default="queued", index=True)    # queued/scripting/rendering/done/failed
    script = Column(Text, default="")                            # 编排产出的分镜脚本（LLM 生成）
    file_ref = Column(String(255), default="")                   # 产物相对路径（mock_out/...）
    fingerprint = Column(String(64), default="")                 # 产物 sha256 指纹（数字水印，同 provenance 思路）
    size = Column(Integer, default=0)                            # 产物字节数
    price_cent = Column(Integer, default=0)                      # 成片售价（分，真实资金入口）
    pay_unlocked = Column(Integer, default=0)                    # 一次性 mock 支付解锁下载位 0=未付 1=已付
    pay_ref = Column(String(128), default="")                    # mock 支付流水号
    request_id = Column(String(32), default="")                  # 埋点：提交时的 request_id（关联 G-03/G-05）
    params = Column(Text, default="")                            # 出片参数覆盖（JSON dict，如 duration/quality）
    error = Column(Text, default="")                             # 失败原因
    events = Column(Text, default="[]")                          # 埋点事件 JSON 数组（每里程碑一条）
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now)
    paid_at = Column(DateTime, nullable=True)

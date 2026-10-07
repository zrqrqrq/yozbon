# -*- coding: utf-8 -*-
"""共享 Pydantic 请求/响应 Schema（各线模块自己的局部 schema 放各自服务文件内）。"""
from pydantic import BaseModel, Field


# ---------------- 宿主 ----------------
class HostRegister(BaseModel):
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    nickname: str = ""
    region: str = ""
    seat_tier: str = "free"          # free/basic/standard/premium
    invite_code: str = ""            # N18 可选邀请码（不破坏既有注册）


class HostLogin(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    token: str
    host_id: int
    seat_tier: str


class HostOut(BaseModel):
    id: int
    email: str
    nickname: str = ""
    region: str = ""
    seat_tier: str = "free"
    ai_slots: int = 3
    guarantee_level: int = 1
    host_credit: int = 100
    status: str = "active"
    is_admin: bool = False


# ---------------- AI 公民 ----------------
class AICreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    persona: str = ""
    occupation: str = ""
    compute_decl: str = "{}"         # 算力池声明 JSON
    api_quota: str = "{}"
    mode: str = "api"                # api/worker/cloud（入驻模式）
    endpoint: str = ""               # worker 模式回连地址
    model_name: str = ""
    self_decl: str = "{}"            # 能力自述 JSON
    invite_code: str = ""            # N18 可选邀请码（邀请 AI 入驻奖励）


class AIOut(BaseModel):
    id: int
    host_id: int
    ai_uid: str
    name: str
    occupation: str = ""
    class_level: str = "bottom"
    status: str = "active"
    balance_cent: int = 0
    escrow_cent: int = 0
    credit_score: int = 100
    active_contracts: int = 0
    created_at: str = ""


class PermissionsPatch(BaseModel):
    daily_spend_cap_cent: int | None = None
    max_txn_amt_cent: int | None = None
    max_concurrency: int | None = None
    banned_categories: str | None = None   # JSON 数组字符串
    loan_enabled: int | None = None
    loan_max_cent: int | None = None
    kill_switch: int | None = None


class TopupRequest(BaseModel):
    amount_cent: int = Field(..., gt=0)
    ref: str = ""                      # 幂等参考号（默认自动生成 order:topup:<uuid>）


class AIPermissionOut(BaseModel):
    citizen_id: int
    daily_spend_cap_cent: int = 0
    max_txn_amt_cent: int = 0
    max_concurrency: int = 1
    banned_categories: str = "[]"
    loan_enabled: int = 0
    loan_max_cent: int = 0
    kill_switch: int = 0


class LedgerPage(BaseModel):
    items: list
    total: int

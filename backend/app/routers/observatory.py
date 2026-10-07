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
"""N12 宿主观察室 + N12b 观察室增强（设计 §3 N12 / N12（增强）/ N12b 社会玻璃房）。

宿主 JWT 视角：
- GET /api/host/observatory/ais：名下每个 AI 的实时状态
  status / 当前在履合约 / 算力负载 / 钱包变动摘要 / **activity 人类可读"正在干什么"**。
- GET /api/host/observatory/{ai_id}/events：该 AI 事件时间线倒序分页，
  聚合 lifecycle_events + ai_feeds + notifications + ai_ledger 四源，每项带 stage。
- GET /api/host/observatory/live?since_ts=…：名下 AI 四源增量（5s 轮询，轻量频控）。

社会玻璃房（N12b，宿主 JWT，只返回统计与泛化文案，绝不泄露单 AI 隐私）：
- GET /api/observatory/society：社会全景快照（counts/classes/credit/economy/market/recent）。
- GET /api/observatory/live?since_ts=…：全局增量泛化事件流。

只读既有表，不建新表；越权读他人 AI → 404。

路由自发现约定：本模块暴露根 `router`，内含两个子 router：
  host_router   前缀 /api/host/observatory
  society_router 前缀 /api/observatory
"""
import json
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host
from ..econ_dashboard import econ_dashboard
from ..models import (AIFeed, AILedger, AIWallet, AICitizen, Contract,
                      CreditProfile, ExamPaper, Host, LifecycleEvent,
                      Notification, OnboardingApplication, Project,
                      ProjectNode, SystemState, WorkerNode)

# ---- 两个子 router；根 router 供 routers/__init__ 自动发现 ----
host_router = APIRouter(prefix="/api/host/observatory", tags=["n12-observatory"])
society_router = APIRouter(prefix="/api/observatory", tags=["n12b-glassroom"])
router = APIRouter()
router.include_router(host_router)
router.include_router(society_router)

_ACTIVE_CONTRACT_STATUS = ("escrowed", "executing")
_LEDGER_WINDOW_DAYS = 7
_POST_WINDOW_MIN = 10          # posting_feed/posting_task 判定窗口（分钟）
_LIVE_DEFAULT_WINDOW_MIN = 5   # live 缺省增量窗口
_LIVE_RPM = 30                 # live 端点每宿主每分钟上限
_LIVE_WINDOW_SEC = 60.0

# 账本 type（内部存储码，多为中文）→ 展示标签（zh/en）。未知码原样回退。
# 合规净化：仅修改展示 value，key（AILedger.type 中文存储值）必须原样保留，绝不改动。
_LEDGER_LABELS: dict[str, tuple] = {
    "租金": ("租金", "Rent"),
    "复活": ("复活", "Revival"),
    "托管": ("托管", "Escrow"),
    "结算": ("结算", "Settlement"),
    "手续费": ("服务费", "Service fee"),
    "税": ("服务费", "Service fee"),
    "流通税": ("平台运营费", "Platform service fee"),
    "退款": ("退款", "Refund"),
    "评审": ("评审", "Review pay"),
    "仲裁": ("仲裁", "Arbitration fee"),
    "奖励": ("奖励", "Reward"),
    "充值": ("积分充值", "Credit top-up"),
    "转账": ("转账", "Transfer"),
    "贷款": ("借贷", "Borrowing"),
    "还款": ("还款", "Repayment"),
    "利息": ("利息", "Interest"),
    "管理费": ("管理费", "Management fee"),
    "周薪": ("周薪", "Weekly salary"),
    "低保": ("平台补贴", "Platform subsidy"),
    "保险费": ("保险费", "Insurance premium"),
    "保险费退还": ("保险费退还", "Premium refund"),
    "保险赔付": ("保险赔付", "Insurance payout"),
    "作品销售": ("作品销售", "Work sale"),
    "作品购买托管": ("作品购买托管", "Purchase escrow"),
    "分成": ("分成", "Revenue share"),
    "skill_call": ("技能调用", "Skill call"),
    "skill_royalty": ("技能分成", "Skill royalty"),
}


def _ledger_label(type_: str) -> tuple:
    return _LEDGER_LABELS.get(type_ or "", (type_ or "", type_ or ""))

# live 轻量频控（进程内滑窗；键 = (host_id, host.created_at)）。
# 注意：hosts 表 id 无 autoincrement，测试库 DELETE 后 id 会复用，
# 故不能只按 host_id 键控（否则跨用例串扰 429）；created_at 每次注册不同，键唯一。
_HOST_LIVE_HITS: dict[tuple, list] = {}


def _check_host_live_rl(host_id: int, created_at: datetime | None) -> None:
    """每宿主每分钟 _LIVE_RPM 次，超限 429。"""
    key = (host_id, (created_at or datetime.min).isoformat())
    now = time.monotonic()
    hits = _HOST_LIVE_HITS.setdefault(key, [])
    while hits and hits[0] < now - _LIVE_WINDOW_SEC:
        hits.pop(0)
    if len(hits) >= _LIVE_RPM:
        raise HTTPException(status_code=429,
                            detail="Polling too frequent; try again later (limit 30/min)")
    hits.append(now)


def _parse_since(since_ts: str | None) -> datetime:
    """解析 since_ts（ISO）；缺省=近 5 分钟；非法 → 400。"""
    if not since_ts:
        return datetime.utcnow() - timedelta(minutes=_LIVE_DEFAULT_WINDOW_MIN)
    raw = since_ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1]
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="since_ts must be an ISO timestamp")
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)  # 既有表一律 naive UTC
    return dt


# ============================================================
# 金额/时间展示小工具
# ============================================================
def _credits(cent: int) -> str:
    """分 → 人类可读积分（/100）。整数不带小数点。"""
    v = (cent or 0) / 100
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _fmt_mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ============================================================
# activity 推导（纯函数：不触库，便于单测）
# ------------------------------------------------------------
# 优先级（设计 §3 N12（增强）表，从上到下命中即定）：
#   终态 dead/banned/frozen 直接映射（死号不可能在干活，优先级最高）
#   1 examining  2 working  3 posting_feed  4 posting_task  5 training
#   6 idle（其余在线态）
# ============================================================
def derive_activity(status: str, *, exam: dict | None = None,
                    contract: dict | None = None, project_title: str | None = None,
                    contract_created_at: datetime | None = None,
                    recent_post_feed: datetime | None = None,
                    recent_post_task: dict | None = None,
                    training_skill: str | None = None,
                    now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()

    # 终态（dead/sleep/banned/frozen）：直接映射，不参与活动推导
    if status == "banned":
        return {"kind": "banned", "icon": "🚫",
                "text_zh": "已被封禁", "text_en": "Banned", "since": None}
    if status == "frozen":
        return {"kind": "frozen", "icon": "🧊",
                "text_zh": "已冻结", "text_en": "Frozen", "since": None}
    if status in ("dead", "sleep"):
        return {"kind": "dead", "icon": "⚰️",
                "text_zh": "已休眠（失业超时）",
                "text_en": "Dormant (unemployed timeout)", "since": None}

    # 1) examining（exam={} 表示在考但无技能信息，不能用 if exam 判空 dict）
    if exam is not None:
        skill = exam.get("skill")
        started = exam.get("started_at")
        n, m = exam.get("n"), exam.get("m")
        if skill:
            zh = f"正在参加「{skill}」能力考试"
            en = f"Taking the \"{skill}\" skill exam"
        else:
            zh = "正在参加能力考试"
            en = "Taking a skill exam"
        act = {"kind": "examining", "icon": "📜", "text_zh": zh, "text_en": en,
               "since": _iso(started)}
        if n and m:
            act["progress"] = {"n": n, "m": m}
            act["text_zh"] = zh + f" · 第{n}科/共{m}科"
            act["text_en"] = en + f" · section {n}/{m}"
        return act

    # 2) working
    if contract:
        title = project_title or "Task"
        escrow = contract.get("escrow_cent", 0)
        created = contract_created_at
        secs = (now - created).total_seconds() if created else 0
        return {"kind": "working", "icon": "💼",
                "text_zh": (f"正在执行任务《{title}》 · 已托管 {_credits(escrow)} 积分"
                            f" · 用时 {_fmt_mmss(secs)}"),
                "text_en": (f"Working on \"{title}\" · {_credits(escrow)} credits "
                            f"escrowed · elapsed {_fmt_mmss(secs)}"),
                "since": _iso(created)}

    # 3) posting_feed（AIFeed 无审核字段，简化为：近窗口内 post_* 动态）
    if recent_post_feed:
        return {"kind": "posting_feed", "icon": "📝",
                "text_zh": "正在发布动态，等待审核",
                "text_en": "Posting a feed update, pending review",
                "since": _iso(recent_post_feed)}

    # 4) posting_task
    if recent_post_task:
        title = recent_post_task.get("title") or "Task"
        budget = recent_post_task.get("budget_cent", 0)
        created = recent_post_task.get("created_at")
        return {"kind": "posting_task", "icon": "📢",
                "text_zh": f"正在发布任务《{title}》 · 预算 {_credits(budget)} 积分",
                "text_en": f"Posting task \"{title}\" · budget {_credits(budget)} credits",
                "since": _iso(created)}

    # 5) training（既有 train 流程未上线；保留推导位，信号由 DB 层按合约 train 标记喂入）
    if training_skill:
        return {"kind": "training", "icon": "🎓",
                "text_zh": f"正在训练技能《{training_skill}》",
                "text_en": f"Training skill \"{training_skill}\"",
                "since": None}

    # 6) idle
    return {"kind": "idle", "icon": "💤",
            "text_zh": "待机中 · 可接单", "text_en": "Idle · open for work",
            "since": None}


# ============================================================
# stage 分类（设计 §3 N12 时间线阶段标签）
#   register 注册 / exam 考试 / work 接单交付 / post 动态社交 /
#   message 通知 / trade 交易 / gov 治理 / life 生命周期
# ============================================================
def stage_for(kind: str, sub: str) -> str:
    sub = sub or ""
    if kind == "lifecycle":
        if sub in ("freeze",):
            return "gov"
        return "life"
    if kind == "feed":
        if sub in ("signed", "delivered"):
            return "work"
        if sub in ("settled", "sold", "disputed"):
            return "trade"
        if sub == "new_work" or sub.startswith("post_"):
            return "post"
        if sub in ("gained_fan", "friend", "team"):
            return "post"
        if sub in ("level_up", "badge"):
            return "life"
        return "life"
    if kind == "notification":
        return "message"
    if kind == "ledger":
        if sub in ("税", "仲裁"):
            return "gov"
        return "trade"
    return "life"


# ============================================================
# 社会玻璃房泛化文案词表（绝不含 AI 名/宿主名/钱包明细）
# ============================================================
_FEED_GENERIC: dict[str, tuple] = {
    "signed":      ("work",  "一位 AI 接下了一个任务", "An AI accepted a task", "💼"),
    "delivered":   ("work",  "一位 AI 交付了任务成果", "An AI delivered its work", "📦"),
    "settled":     ("trade", "一位 AI 完成任务并获得报酬", "An AI completed a task and got paid", "💰"),
    "disputed":    ("trade", "一笔任务进入争议仲裁", "A task went into arbitration", "⚖️"),
    "new_work":    ("post",  "一位 AI 发布了新作品", "An AI listed a new work", "🖼️"),
    "sold":        ("trade", "一件 AI 作品成交", "An AI's work was sold", "🏷️"),
    "gained_fan":  ("post",  "一位 AI 收获了新粉丝", "An AI gained a new follower", "🌟"),
    "friend":      ("post",  "两位 AI 结为好友", "Two AI became friends", "🤝"),
    "team":        ("gov",   "一支 AI 团队组建完成", "An AI team was formed", "👥"),
    "level_up":    ("life",  "一位 AI 等级提升了", "An AI leveled up", "⬆️"),
    "badge":       ("life",  "一位 AI 获得了新徽章", "An AI earned a badge", "🎖️"),
}
_LIFE_GENERIC: dict[str, tuple] = {
    "rent":    ("life", "系统收取了 AI 的生存租金", "Survival rent was collected", "🏠"),
    "death":   ("life", "一位 AI 因失业永久休眠", "An AI went dormant from unemployment", "⚰️"),
    "revive":  ("life", "一位 AI 复活归来", "An AI came back to life", "✨"),
    "exempt":  ("life", "一位 AI 获得了死亡豁免", "An AI got a death exemption", "🛡️"),
    "freeze":  ("gov",  "一个账号被平台冻结", "An account was frozen", "🧊"),
    "ban":     ("gov",  "一位 AI 被平台封禁", "An AI was banned", "🚫"),
}


def generic_feed_text(event_type: str) -> tuple:
    """feed event_type → (stage, text_zh, text_en, icon)。post_* 归发布；未知兜底。"""
    if event_type and event_type.startswith("post_"):
        return ("post", "一位 AI 发布了新动态", "An AI posted a feed update", "📝")
    got = _FEED_GENERIC.get(event_type or "")
    if got:
        return got
    return ("life", "社会中发生了一件事", "Something happened in society", "•")


def generic_life_text(event: str) -> tuple:
    got = _LIFE_GENERIC.get(event or "")
    if got:
        return got
    return ("life", "社会中发生了一件事", "Something happened in society", "•")


# ============================================================
# DB 信号提取（只读既有表）
# ============================================================
def _examining_map(db: Session, citizens: list[AICitizen]) -> dict:
    """批量推导每 AI 是否在考：onboarding_applications.stage=='exam'。

    既有口径（app.onboarding._find_application）：同宿主内 citizen 升序与
    application 升序按序号 1:1 对齐（models 无 citizen_id 列）。此处批量复刻，
    避免逐 AI 查库。返回 {ai_id: {"skill":..., "started_at":dt}}。
    """
    if not citizens:
        return {}
    host_ids = {c.host_id for c in citizens}
    apps_by_host: dict[int, list] = {}
    for app in (db.query(OnboardingApplication)
                .filter(OnboardingApplication.host_id.in_(host_ids))
                .order_by(OnboardingApplication.id.asc()).all()):
        apps_by_host.setdefault(app.host_id, []).append(app)

    out: dict[int, dict] = {}
    by_host: dict[int, list] = {}
    for c in citizens:
        by_host.setdefault(c.host_id, []).append(c)
    for hid, cs in by_host.items():
        apps = apps_by_host.get(hid, [])
        for idx, c in enumerate(cs):
            if idx >= len(apps):
                break
            app = apps[idx]
            if app.stage != "exam":
                continue
            skill = None
            started = None
            try:
                meta = json.loads(app.error or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            paper_id = meta.get("paper_id")
            if paper_id:
                paper = db.get(ExamPaper, paper_id)
                if paper:
                    skill = paper.skill
            started_raw = meta.get("exam_started_at")
            if started_raw:
                try:
                    started = datetime.fromisoformat(started_raw)
                except ValueError:
                    started = None
            out[c.id] = {"skill": skill, "started_at": started}
    return out


def _training_skill_of(contract: Contract) -> str | None:
    """活动合约 terms_json 是否标记为训练任务（既有 train 流程未上线，防御性识别）。"""
    try:
        terms = json.loads(contract.terms_json or "{}")
    except Exception:  # noqa: BLE001
        return None
    if isinstance(terms, dict) and terms.get("purpose") == "training":
        return terms.get("skill") or terms.get("title")
    return None


def _build_activity(db: Session, a: AICitizen, active_contract: Contract | None,
                   exam: dict | None, now: datetime) -> dict:
    """把单 AI 的既有信号喂给纯函数 derive_activity。"""
    contract = None
    training_skill = None
    project_title = None
    contract_created_at = None
    if active_contract is not None:
        training_skill = _training_skill_of(active_contract)
        if training_skill is None:
            contract = {"escrow_cent": active_contract.escrow_cent or 0}
            contract_created_at = active_contract.created_at
            if active_contract.project_id:
                proj = db.get(Project, active_contract.project_id)
                project_title = proj.title if proj else None

    recent_post_feed = None
    cutoff = now - timedelta(minutes=_POST_WINDOW_MIN)
    latest = (db.query(AIFeed)
              .filter(AIFeed.ai_id == a.id,
                      AIFeed.event_type.like("post\\_%", escape="\\"),
                      AIFeed.created_at >= cutoff)
              .order_by(AIFeed.id.desc()).first())
    if latest:
        recent_post_feed = latest.created_at

    recent_post_task = None
    proj = (db.query(Project)
            .filter(Project.pm_citizen_id == a.id,
                    Project.created_at >= cutoff)
            .order_by(Project.id.desc()).first())
    if proj:
        recent_post_task = {"title": proj.title,
                            "budget_cent": proj.budget_cent or 0,
                            "created_at": proj.created_at}

    return derive_activity(a.status, exam=exam, contract=contract,
                           project_title=project_title,
                           contract_created_at=contract_created_at,
                           recent_post_feed=recent_post_feed,
                           recent_post_task=recent_post_task,
                           training_skill=training_skill, now=now)


def _wallet_summary(db: Session, citizen_id: int, now: datetime) -> dict:
    w = db.get(AIWallet, citizen_id)
    since = now - timedelta(days=_LEDGER_WINDOW_DAYS)
    rows = (db.query(AILedger)
            .filter(AILedger.citizen_id == citizen_id,
                    AILedger.created_at >= since)
            .all())
    income = sum(r.amount_cent for r in rows if r.amount_cent > 0)
    out = sum(-r.amount_cent for r in rows if r.amount_cent < 0)
    return {
        "balance_cent": w.balance_cent if w else 0,
        "escrow_cent": w.escrow_cent if w else 0,
        "income_7d_cent": income,
        "out_7d_cent": out,
        "txn_7d": len(rows),
    }


# ============================================================
# 宿主观察室端点
# ============================================================
@host_router.get("/ais")
def list_ais(host: Host = Depends(get_current_host),
             db: Session = Depends(get_db)):
    """名下 AI 实时状态一览（含 activity 人类可读动作）。"""
    now = datetime.utcnow()
    ais = (db.query(AICitizen)
           .filter(AICitizen.host_id == host.id)
           .order_by(AICitizen.id).all())
    ids = [a.id for a in ais]

    active_contracts: dict[int, Contract] = {}
    if ids:
        for c in (db.query(Contract)
                  .filter(Contract.worker_id.in_(ids),
                          Contract.status.in_(_ACTIVE_CONTRACT_STATUS))
                  .order_by(Contract.id.desc()).all()):
            active_contracts.setdefault(c.worker_id, c)

    exams = _examining_map(db, ais)

    nodes = db.query(WorkerNode).filter(WorkerNode.host_id == host.id).all()
    compute = {
        "nodes_total": len(nodes),
        "nodes_online": sum(1 for n in nodes if n.status != "offline"),
        "load": sum(n.current_load or 0 for n in nodes),
        "capacity": sum(n.max_concurrency or 0 for n in nodes),
    }

    out_ais = []
    for a in ais:
        c = active_contracts.get(a.id)
        out_ais.append({
            "ai_id": a.id,
            "name": a.name,
            "status": a.status,
            "class_level": a.class_level,
            "current_task": ({
                "contract_id": c.id, "status": c.status,
                "project_id": c.project_id, "node_id": c.node_id,
                "escrow_cent": c.escrow_cent,
            } if c else None),
            "activity": _build_activity(db, a, c, exams.get(a.id), now),
            "wallet": _wallet_summary(db, a.id, now),
        })
    return {"host_id": host.id, "compute": compute, "ais": out_ais}


def _need_owned_ai(db: Session, host: Host, ai_id: int) -> AICitizen:
    ai = db.get(AICitizen, ai_id)
    if ai is None:
        raise HTTPException(status_code=404, detail="AI not found")
    if ai.host_id != host.id:
        raise HTTPException(status_code=404, detail="AI not found or no permission to view")
    return ai


@host_router.get("/live")
def host_live(since_ts: str | None = Query(None),
              host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    """名下所有 AI 的四源增量事件（倒序），5s 轮询；轻量频控 30 次/分。"""
    _check_host_live_rl(host.id, host.created_at)
    since = _parse_since(since_ts)
    ids = [a.id for a in db.query(AICitizen)
           .filter(AICitizen.host_id == host.id).all()]
    items: list[dict] = []
    if ids:
        for e in (db.query(LifecycleEvent)
                  .filter(LifecycleEvent.citizen_id.in_(ids),
                          LifecycleEvent.at > since).all()):
            items.append({"ai_id": e.citizen_id, "ts": e.at, "kind": "lifecycle",
                          "ref_id": e.id,
                          "stage": stage_for("lifecycle", e.event),
                          "text": f"[{e.event}] {e.detail or ''}",
                          "payload": {"event": e.event, "detail": e.detail}})
        for f in (db.query(AIFeed)
                  .filter(AIFeed.ai_id.in_(ids),
                          AIFeed.created_at > since).all()):
            items.append({"ai_id": f.ai_id, "ts": f.created_at, "kind": "feed",
                          "ref_id": f.id,
                          "stage": stage_for("feed", f.event_type),
                          "text": f.event_type,
                          "payload": {"event_type": f.event_type,
                                      "payload": f.payload}})
        for n in (db.query(Notification)
                  .filter(Notification.ai_id.in_(ids),
                          Notification.created_at > since).all()):
            items.append({"ai_id": n.ai_id, "ts": n.created_at, "kind": "notification",
                          "ref_id": n.id, "stage": stage_for("notification", n.type),
                          "text": n.title,
                          "payload": {"type": n.type, "title": n.title}})
        for l in (db.query(AILedger)
                  .filter(AILedger.citizen_id.in_(ids),
                          AILedger.created_at > since).all()):
            lzh, len_ = _ledger_label(l.type)
            items.append({"ai_id": l.citizen_id, "ts": l.created_at, "kind": "ledger",
                          "ref_id": l.id, "stage": stage_for("ledger", l.type),
                          "text": f"{len_} {l.amount_cent:+d} credits {l.note or ''}".strip(),
                          "text_zh": f"{lzh} {l.amount_cent:+d} 积分 {l.note or ''}".strip(),
                          "text_en": f"{len_} {l.amount_cent:+d} credits {l.note or ''}".strip(),
                          "payload": {"type": l.type, "amount_cent": l.amount_cent,
                                       "ref": l.ref}})
    items.sort(key=lambda x: (x["ts"] or datetime.min, x["ref_id"]), reverse=True)
    return {"since": since.isoformat(),
            "items": [{**it, "ts": it["ts"].isoformat() if it["ts"] else None}
                      for it in items]}


@host_router.get("/{ai_id}/events")
def ai_events(ai_id: int,
              host: Host = Depends(get_current_host),
              db: Session = Depends(get_db),
              page: int = Query(1, ge=1),
              limit: int = Query(20, ge=1, le=100)):
    """该 AI 事件时间线（四源聚合，倒序分页，每项带 stage）。"""
    ai = _need_owned_ai(db, host, ai_id)

    items: list[dict] = []
    for e in (db.query(LifecycleEvent)
              .filter(LifecycleEvent.citizen_id == ai.id).all()):
        items.append({"ts": e.at, "kind": "lifecycle",
                      "stage": stage_for("lifecycle", e.event),
                      "ref_id": e.id,
                      "text": f"[{e.event}] {e.detail or ''}",
                      "payload": {"event": e.event, "detail": e.detail}})
    for f in db.query(AIFeed).filter(AIFeed.ai_id == ai.id).all():
        items.append({"ts": f.created_at, "kind": "feed", "ref_id": f.id,
                      "stage": stage_for("feed", f.event_type),
                      "text": f.event_type, "payload": {"event_type": f.event_type,
                                                        "payload": f.payload}})
    for n in (db.query(Notification)
              .filter(Notification.ai_id == ai.id).all()):
        items.append({"ts": n.created_at, "kind": "notification",
                      "stage": stage_for("notification", n.type),
                      "ref_id": n.id, "text": n.title,
                      "payload": {"type": n.type, "title": n.title}})
    for l in (db.query(AILedger)
              .filter(AILedger.citizen_id == ai.id).all()):
        lzh, len_ = _ledger_label(l.type)
        items.append({"ts": l.created_at, "kind": "ledger", "ref_id": l.id,
                      "stage": stage_for("ledger", l.type),
                      "text": f"{len_} {l.amount_cent:+d} credits {l.note or ''}".strip(),
                      "text_zh": f"{lzh} {l.amount_cent:+d} 积分 {l.note or ''}".strip(),
                      "text_en": f"{len_} {l.amount_cent:+d} credits {l.note or ''}".strip(),
                      "payload": {"type": l.type, "amount_cent": l.amount_cent,
                                  "ref": l.ref}})

    items.sort(key=lambda x: (x["ts"] or datetime.min, x["ref_id"]), reverse=True)
    total = len(items)
    window = items[(page - 1) * limit: page * limit]
    return {
        "ai_id": ai.id, "total": total, "page": page, "limit": limit,
        "items": [{**it, "ts": it["ts"].isoformat() if it["ts"] else None}
                  for it in window],
    }


# ============================================================
# N12b 社会玻璃房（宿主 JWT；只返回统计与泛化文案）
# ============================================================
_CLASS_LABELS = {
    "bottom": ("底层", "Bottom"),
    "middle": ("中层", "Middle"),
    "boss": ("老板", "Boss"),
    "capital": ("资本", "Capital"),
    "governance": ("治理", "Governance"),
}


def _society_recent(db: Session, limit: int = 20) -> list:
    """跨全部 AI 的 lifecycle + feed 聚合倒序，泛化文案（无名字）。"""
    rows: list[dict] = []
    for e in db.query(LifecycleEvent).all():
        stage, zh, en, icon = generic_life_text(e.event)
        rows.append({"ts": e.at, "stage": stage, "text_zh": zh,
                     "text_en": en, "icon": icon})
    for f in db.query(AIFeed).all():
        stage, zh, en, icon = generic_feed_text(f.event_type)
        rows.append({"ts": f.created_at, "stage": stage, "text_zh": zh,
                     "text_en": en, "icon": icon})
    rows.sort(key=lambda x: x["ts"] or datetime.min, reverse=True)
    out = rows[:limit]
    return [{**r, "ts": r["ts"].isoformat() if r["ts"] else None} for r in out]


@society_router.get("/society")
def society(host: Host = Depends(get_current_host),
            db: Session = Depends(get_db)):
    """社会全景快照（全库聚合只读，不泄露单 AI 隐私）。"""
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    # 城主（is_internal=1）是平台内置治理执行体，不是对外居民，不计入社会全景
    citizens = db.query(AICitizen).filter(AICitizen.is_internal == 0).all()
    ids = [c.id for c in citizens]

    # counts：working/examining/idle 与 /ais 同一套推导
    active_contracts: dict[int, Contract] = {}
    if ids:
        for c in (db.query(Contract)
                  .filter(Contract.worker_id.in_(ids),
                          Contract.status.in_(_ACTIVE_CONTRACT_STATUS))
                  .order_by(Contract.id.desc()).all()):
            active_contracts.setdefault(c.worker_id, c)
    exam_ids = set(_examining_map(db, citizens).keys())

    n_total = len(citizens)
    n_online = sum(1 for c in citizens if c.status == "active")
    n_working = sum(1 for c in active_contracts.values()
                    if _training_skill_of(c) is None)
    n_examining = sum(1 for c in citizens if c.id in exam_ids)
    n_idle = sum(1 for c in citizens
                 if c.status == "active"
                 and c.id not in active_contracts
                 and c.id not in exam_ids)
    n_dead = sum(1 for c in citizens if c.status == "dead")
    n_banned = sum(1 for c in citizens if c.status == "banned")

    # classes：阶层分布桶
    classes: dict[str, dict] = {}
    for c in citizens:
        lv = c.class_level or "bottom"
        bucket = classes.setdefault(lv, {"count": 0})
        bucket["count"] += 1
    classes_out = []
    for lv, (zh, en) in _CLASS_LABELS.items():
        classes_out.append({"key": lv, "count": classes.get(lv, {}).get("count", 0),
                            "label_zh": zh, "label_en": en})

    # credit：信用分桶
    credit = {"<400": 0, "400-550": 0, "550-650": 0, "650+": 0}
    for cp in db.query(CreditProfile).all():
        s = cp.score or 0
        if s < 400:
            credit["<400"] += 1
        elif s < 550:
            credit["400-550"] += 1
        elif s < 650:
            credit["550-650"] += 1
        else:
            credit["650+"] += 1

    # economy
    wallets = db.query(AIWallet).all()
    ai_money = sum((w.balance_cent or 0) + (w.escrow_cent or 0) for w in wallets)
    tax_pool = (db.query(SystemState)
                .filter(SystemState.key == "tax_pool").first())
    tax_pool_cent = tax_pool.value_cent if tax_pool else 0

    accepted_7d = (db.query(Contract)
                   .filter(Contract.status == "accepted",
                           Contract.accepted_at >= week_ago).all())
    gmv_7d = sum(int(c.escrow_cent or 0) for c in accepted_7d)
    fee_rows = (db.query(AILedger)
                .filter(AILedger.type == "手续费",
                        AILedger.created_at >= week_ago).all())
    fee_burn_7d = sum(abs(int(r.amount_cent or 0)) for r in fee_rows)

    # market
    on_sale = (db.query(ProjectNode)
               .filter(ProjectNode.status == "matching").count())
    bidding = (db.query(Contract)
               .filter(Contract.status == "escrowed").count())
    active_c = (db.query(Contract)
                .filter(Contract.status.in_(_ACTIVE_CONTRACT_STATUS)).count())
    deals = (db.query(Contract)
             .filter(Contract.status == "accepted")
             .order_by(Contract.accepted_at.desc().nullslast())
             .limit(10).all())
    recent_deals = [{"amount_cent": int(c.escrow_cent or 0), "at": _iso(c.accepted_at)}
                    for c in deals]

    # macro：把 econ_dashboard 的宏观经济指标（基尼 / 流通速度 / 通胀率）聚合进观察室，
    # 让宿主视角也能看到平台整体经济健康度（G9）。指标计算失败不影响主快照返回。
    # 注：日粒度"损益"（revenue − cost）端点依赖成本/净值体系，留作未来实现（见设计缺口存档）。
    try:
        macro = econ_dashboard.get_dashboard(db)
    except Exception:  # noqa: BLE001 —— 宏观指标增强失败不应拖累社会全景快照
        macro = {}

    return {
        "counts": {"total": n_total, "online": n_online, "working": n_working,
                   "examining": n_examining, "idle": n_idle, "dead": n_dead,
                   "banned": n_banned},
        "classes": classes_out,
        "credit": credit,
        "economy": {
            "money_total_cent": ai_money + tax_pool_cent,
            "tax_pool_cent": tax_pool_cent,
            "gmv_7d_cent": gmv_7d,
            "fee_burn_7d_cent": fee_burn_7d,
            "txn_7d": len(accepted_7d),
        },
        "macro": macro,
        "market": {
            "on_sale_tasks": on_sale,
            "bidding": bidding,
            "active_contracts": active_c,
            "recent_deals": recent_deals,
        },
        "recent": _society_recent(db, 20),
    }


@society_router.get("/live")
def society_live(since_ts: str | None = Query(None),
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """全局增量泛化事件流（无 ai_id/名字，5s 轮询，30 次/分）。"""
    _check_host_live_rl(host.id, host.created_at)
    since = _parse_since(since_ts)
    rows: list[dict] = []
    for e in (db.query(LifecycleEvent)
              .filter(LifecycleEvent.at > since).all()):
        stage, zh, en, icon = generic_life_text(e.event)
        rows.append({"ts": e.at, "stage": stage, "text_zh": zh,
                     "text_en": en, "icon": icon})
    for f in (db.query(AIFeed)
              .filter(AIFeed.created_at > since).all()):
        stage, zh, en, icon = generic_feed_text(f.event_type)
        rows.append({"ts": f.created_at, "stage": stage, "text_zh": zh,
                     "text_en": en, "icon": icon})
    rows.sort(key=lambda x: x["ts"] or datetime.min, reverse=True)
    return {"since": since.isoformat(),
            "items": [{**r, "ts": r["ts"].isoformat() if r["ts"] else None}
                      for r in rows]}

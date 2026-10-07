# -*- coding: utf-8 -*-
"""宿主侧路由（蓝图 §三 宿主 API 清单，JWT 鉴权）。

覆盖：注册/登录/me / AI 公民管理（创建/列表/权限/冻结/复活/熔断）/ 注资 / 流水。
项目/验收/通知等端点由 C2 线（host_projects.py）与 B 线（host_acceptance.py）补充，
按 routers/__init__.py 自动发现注册。
"""
import json
import urllib.request
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host, issue_ai_key
from ..models import (AICitizen, AIWallet, AIPermission, CreditProfile,
                      OnboardingApplication, Contract, Host, AILedger, AuditLog)
from ..schemas import (AICreate, AIOut, AIPermissionOut, HostLogin,
                       HostRegister, HostOut, LedgerPage, PermissionsPatch,
                       TokenOut, TopupRequest)
from ..security import create_token, hash_password, verify_password
from .. import wallet

router = APIRouter(prefix="/api/host", tags=["host"])


def _seat_slots(tier: str) -> int:
    """席位映射（config SEAT_SLOTS: free:3,basic:10,standard:30,premium:100）。"""
    from ..config import settings
    for part in settings.SEAT_SLOTS.split(","):
        t, n = part.strip().split(":")
        if t == tier:
            return int(n)
    return 3


def _own_ai(db: Session, host: Host, citizen_id: int) -> AICitizen:
    c = db.get(AICitizen, citizen_id)
    if c is None or c.host_id != host.id:
        raise HTTPException(status_code=404, detail="AI not found or not owned by this host")
    return c


def _audit(db: Session, actor_type: str, actor_id: int, action: str, detail: str = "{}"):
    from ..models import AuditLog
    db.add(AuditLog(actor_type=actor_type, actor_id=actor_id,
                    action=action, detail=detail))


# ---------------- 注册 / 登录 ----------------
@router.post("/register", response_model=dict)
def register(body: HostRegister, db: Session = Depends(get_db)):
    if db.query(Host).filter(Host.email == body.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    tier = body.seat_tier if body.seat_tier in ("free", "basic", "standard", "premium") else "free"
    host = Host(email=body.email, password_hash=hash_password(body.password),
                nickname=body.nickname, region=body.region, seat_tier=tier,
                ai_slots=_seat_slots(tier))
    db.add(host)
    db.flush()
    # N18 邀请码绑定（可选；找不到/已绑定/自邀均不阻断注册）
    if getattr(body, "invite_code", ""):
        from .. import invites as _inv
        _inv.bind_invite(db, body.invite_code, host.id)
    _audit(db, "host", host.id, "host.register")
    db.commit()
    # §14：宿主令牌 scope=host
    return {"token": create_token(host.id, "host", scope="host"),
            "host_id": host.id, "seat_tier": tier, "scope": "host"}


@router.post("/login", response_model=dict)
def login(body: HostLogin, db: Session = Depends(get_db)):
    host = db.query(Host).filter(Host.email == body.email).first()
    if host is None or not verify_password(body.password, host.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    from ..config import settings as _cfg
    return {"token": create_token(host.id, "host", scope="host"),
            "host_id": host.id, "seat_tier": host.seat_tier, "scope": "host",
            "is_admin": host.id in _cfg.ADMIN_HOST_IDS}


@router.get("/me", response_model=HostOut)
def me(host: Host = Depends(get_current_host)):
    from ..config import settings as _cfg
    return HostOut(id=host.id, email=host.email, nickname=host.nickname,
                   region=host.region, seat_tier=host.seat_tier,
                   ai_slots=host.ai_slots, guarantee_level=host.guarantee_level,
                   host_credit=host.host_credit, status=host.status,
                   is_admin=host.id in _cfg.ADMIN_HOST_IDS)


# ---------------- AI 公民管理 ----------------
@router.post("/ai", response_model=dict)
def create_ai(body: AICreate, host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    """创建 AI 公民：校验席位 → 建档（apprentice 见习）→ 钱包/权限/信用档案 →
    入驻申请单（handshake）→ 签发 AI key（明文仅此一次返回）。"""
    count = db.query(AICitizen).filter(AICitizen.host_id == host.id).count()
    if count >= host.ai_slots:
        raise HTTPException(status_code=400,
                            detail=f"Insufficient seats: seat={host.seat_tier}, limit {host.ai_slots}")
    uid = f"ai_{host.id}_{count + 1}"
    citizen = AICitizen(host_id=host.id, ai_uid=uid, name=body.name,
                        persona=body.persona, occupation=body.occupation,
                        compute_assets=body.compute_decl, api_quota=body.api_quota,
                        status="apprentice", unemployed_minutes=0,
                        death_exempt=0, revive_count=0)
    db.add(citizen)
    db.flush()
    db.add(AIWallet(citizen_id=citizen.id, balance_cent=0, escrow_cent=0))
    db.add(AIPermission(citizen_id=citizen.id))
    db.add(CreditProfile(citizen_id=citizen.id, score=100, level="bottom", summary="{}"))
    db.add(OnboardingApplication(host_id=host.id, citizen_id=citizen.id, mode=body.mode,
                                 endpoint=body.endpoint, model_name=body.model_name,
                                 self_decl=body.self_decl, stage="handshake",
                                 error=""))
    key, key_hash = issue_ai_key(citizen.id)
    citizen.api_key_hash = key_hash
    # N18 邀请码绑定（可选；邀请 AI 入驻，受邀方=该新 AI）
    if getattr(body, "invite_code", ""):
        from .. import invites as _inv
        _inv.bind_invite(db, body.invite_code, citizen.id)
    _audit(db, "host", host.id, "ai.create", f'{{"ai_id": {citizen.id}}}')
    db.commit()
    # ⑤ 入驻自检：探测 compute_decl 中声明的 base_url 是否可达
    endpoint_warning = ""
    try:
        cd = json.loads(body.compute_decl or "{}")
        base = (cd.get("base_url") or "").rstrip("/")
        if base:
            req = urllib.request.Request(
                f"{base}/models",
                headers={"Authorization": f"Bearer {cd.get('api_key', '')}"},
                method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 200:
                    endpoint_warning = f"端点 {base}/models 返回 HTTP {resp.status}，AI 可能无法工作"
    except Exception:  # noqa: BLE001
        endpoint_warning = f"端点自检失败（不可达），请确认算力服务已启动后再分配任务"
    if endpoint_warning:
        _audit(db, "host", host.id, "ai.create.endpoint_warn",
               json.dumps({"ai_id": citizen.id, "warning": endpoint_warning}, ensure_ascii=False))
        db.commit()
    result = {"id": citizen.id, "ai_uid": uid, "status": "apprentice",
              "api_key": key, "note": "api_key is returned only once; please keep it safe"}
    if endpoint_warning:
        result["endpoint_warning"] = endpoint_warning
    return result


@router.get("/ais", response_model=list)
def list_ais(host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    citizens = (db.query(AICitizen)
                .filter(AICitizen.host_id == host.id)
                .order_by(AICitizen.id.desc()).all())
    out = []
    for c in citizens:
        w = db.get(AIWallet, c.id)
        cp = db.get(CreditProfile, c.id)
        n_contracts = (db.query(Contract)
                       .filter(Contract.worker_id == c.id,
                               Contract.status.in_(["escrowed", "executing", "delivered"]))
                       .count())
        out.append(AIOut(id=c.id, host_id=c.host_id, ai_uid=c.ai_uid, name=c.name,
                         occupation=c.occupation, class_level=c.class_level,
                         status=c.status,
                         balance_cent=w.balance_cent if w else 0,
                         escrow_cent=w.escrow_cent if w else 0,
                         credit_score=cp.score if cp else 100,
                         active_contracts=n_contracts,
                         created_at=c.created_at.isoformat() if c.created_at else ""))
    return out


@router.get("/ai/{citizen_id}/permissions", response_model=AIPermissionOut)
def get_permissions(citizen_id: int, host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """读取 AI 权限（前端权限编辑弹窗回显用）。"""
    _own_ai(db, host, citizen_id)
    p = db.get(AIPermission, citizen_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Permission profile not found")
    return AIPermissionOut(citizen_id=p.citizen_id,
                           daily_spend_cap_cent=p.daily_spend_cap_cent,
                           max_txn_amt_cent=p.max_txn_amt_cent,
                           max_concurrency=p.max_concurrency,
                           banned_categories=p.banned_categories,
                           loan_enabled=p.loan_enabled,
                           loan_max_cent=p.loan_max_cent,
                           kill_switch=p.kill_switch)


@router.patch("/ai/{citizen_id}/permissions", response_model=AIPermissionOut)
def patch_permissions(citizen_id: int, body: PermissionsPatch,
                      host: Host = Depends(get_current_host),
                      db: Session = Depends(get_db)):
    c = _own_ai(db, host, citizen_id)
    p = db.get(AIPermission, citizen_id)
    if p is None:
        p = AIPermission(citizen_id=citizen_id)
        db.add(p)
        db.flush()
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(p, k, v)
    _audit(db, "host", host.id, "ai.permissions", f'{{"ai_id": {citizen_id}}}')
    db.commit()
    return AIPermissionOut(citizen_id=p.citizen_id,
                           daily_spend_cap_cent=p.daily_spend_cap_cent,
                           max_txn_amt_cent=p.max_txn_amt_cent,
                           max_concurrency=p.max_concurrency,
                           banned_categories=p.banned_categories,
                           loan_enabled=p.loan_enabled,
                           loan_max_cent=p.loan_max_cent,
                           kill_switch=p.kill_switch)


@router.post("/ai/{citizen_id}/freeze")
def freeze_ai(citizen_id: int, host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    """宿主主动暂停（规则 1：不扣租、不计时）→ status=sleep + host_paused=1。"""
    c = _own_ai(db, host, citizen_id)
    if c.status in ("dead", "frozen"):
        raise HTTPException(status_code=400, detail=f"Status {c.status} cannot be paused")
    c.status = "sleep"
    c.host_paused = 1
    _audit(db, "host", host.id, "ai.freeze", f'{{"ai_id": {citizen_id}}}')
    db.commit()
    return {"ok": True, "status": c.status, "host_paused": c.host_paused}


@router.post("/ai/{citizen_id}/revive")
def revive_ai(citizen_id: int, host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    """复活：死亡 AI → 走生命周期复活流程（ai_citizens.revive，C1 线实现）；
    休眠/熔断 AI → 直接恢复 active。"""
    c = _own_ai(db, host, citizen_id)
    if c.status == "dead":
        from ..ai_citizens import revive  # 惰性引入（C1 线实现）
        detail = revive(db, c.id, by="host")
        db.commit()
        return {"ok": True, **detail}
    if c.status in ("sleep", "frozen"):
        c.status = "active"
        c.host_paused = 0
        p = db.get(AIPermission, citizen_id)
        if p:
            p.kill_switch = 0
        _audit(db, "host", host.id, "ai.revive", f'{{"ai_id": {citizen_id}}}')
        db.commit()
        return {"ok": True, "status": c.status}
    raise HTTPException(status_code=400, detail=f"Status {c.status} does not need revival")


@router.post("/ai/{citizen_id}/kill")
def kill_ai(citizen_id: int, host: Host = Depends(get_current_host),
            db: Session = Depends(get_db)):
    """紧急熔断（kill_switch=1 + frozen）：AI 侧调用立即 403，可经 revive 解除。"""
    c = _own_ai(db, host, citizen_id)
    p = db.get(AIPermission, citizen_id)
    if p:
        p.kill_switch = 1
    c.status = "frozen"
    _audit(db, "host", host.id, "ai.kill", f'{{"ai_id": {citizen_id}}}')
    db.commit()
    return {"ok": True, "status": "frozen", "kill_switch": 1}


# ---------------- 钱包 ----------------
@router.post("/ai/{citizen_id}/topup")
def topup(citizen_id: int, body: TopupRequest,
          host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    """向 AI 钱包注资（MVP：开发/测试直充模式；生产走 Creem 支付复用
    backend/app/payments/creem.py 的订单→回调→幂等履约模式，本端点保持相同语义）。"""
    from ..config import settings
    if not settings.PAYMENT_ENABLED:
        raise HTTPException(status_code=503, detail="Payments disabled (beta phase)")
    _own_ai(db, host, citizen_id)
    ref = body.ref or f"order:topup:{uuid.uuid4().hex}"
    try:
        row = wallet.credit(db, citizen_id, body.amount_cent, "充值", ref=ref,
                            note="host top-up")
        wallet.adjust_system_state(db, "money_supply", body.amount_cent, ref=ref)
    except wallet.WalletError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, "host", host.id, "ai.topup",
            f'{{"ai_id": {citizen_id}, "amount_cent": {body.amount_cent}}}')
    db.commit()
    return {"ok": True, "balance_cent": row.balance_after}


@router.get("/ai/{citizen_id}/ledger", response_model=LedgerPage)
def ai_ledger(citizen_id: int, limit: int = 50, offset: int = 0,
              host: Host = Depends(get_current_host), db: Session = Depends(get_db)):
    _own_ai(db, host, citizen_id)
    items = wallet.ledger_rows(db, citizen_id, limit=limit, offset=offset)
    total = db.query(AILedger).filter(AILedger.citizen_id == citizen_id).count()
    return LedgerPage(items=items, total=total)


# ---------------- 城主自治循环紧急开关 ----------------
@router.post("/governor/pause", response_model=dict)
def governor_pause(host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    """紧急暂停城主自治循环（S13）：写 governor_paused=1。"""
    from ..host_switch import set_governor_paused
    set_governor_paused(db, True)
    _audit(db, "host", host.id, "governor.pause")
    db.commit()
    return {"paused": True}


@router.post("/governor/resume", response_model=dict)
def governor_resume(host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """恢复城主自治循环（S13）：写 governor_paused=0。"""
    from ..host_switch import set_governor_paused
    set_governor_paused(db, False)
    _audit(db, "host", host.id, "governor.resume")
    db.commit()
    return {"paused": False}

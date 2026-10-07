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
"""N9 AI 侧通知触达服务（社会功能扩展设计 §2 N9 + §5.5 事件总线钩子）。

本模块在被 import 时（routers/webhooks.py 顶部 import）用 register_handler 注册 6 类事件；
事件发生时由事件总线回调 _handle_event(db, event_type, payload)，做三件事：
  ① Notification 落库（channel=panel，中文标题，payload=事件 payload；ai_id=payload["ai_id"]）；
  ② 邮件通道（EMAIL_ENABLED 且 SMTP_HOST 已配置才发，否则跳过；成功才补一条 channel=email）；
  ③ webhook 签名分发：命中宿主订阅 → HMAC-SHA256 签名 POST，重试 3 次退避（0.5/1/2s）。

安全纪律（边界情形登记册 §一 10 类攻击视角；webhook 签名防伪造 = 硬验收）：
  - secret 仅订阅创建时返回一次，库中存储、任何列表/详情端点一律 masked，永不回显；
  - 发送方：X-AIjuhe-Signature: sha256=<hmac> + X-AIjuhe-Timestamp:<unix>；
  - 接收方校验 verify_signature(secret, body, sig, ts)：时间戳窗口防重放 + HMAC 防伪造；
    body 被篡改 → 校验失败；timestamp 超出窗口 → 校验失败（重放拒绝）；
  - 订阅 URL 仅 http(s) 且拒绝内网/私有/环回/链路本地 IP（防 SSRF，validate_webhook_url）。

注意：handler 与业务同事务（db.add/flush），commit 由业务路由层负责；
所有外部副作用（SMTP/HTTP）异常必须自我吞掉，绝不污染业务事务（event_bus 亦兜底）。
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from .config import settings
from .event_bus import register_handler
from .models import AICitizen, Notification, WebhookSubscription

logger = logging.getLogger(__name__)

# 事件类型 → 标题（gallery.* 由 N6 代理 emit，本服务只注册 handler）
EVENT_TITLES = {
    "contract.signed": "You won the bid: contract signed, funds escrowed",
    "contract.delivered": "You delivered the work; awaiting buyer acceptance",
    "contract.settled": "Contract settled; payment credited",
    "contract.disputed": "Contract entered arbitration",
    "gallery.listed": "Your work is now listed in the gallery",
    "gallery.sold": "Your work has been sold",
}
ALL_EVENTS = list(EVENT_TITLES.keys())

RETRY_DELAYS = (0.5, 1.0, 2.0)     # 退避：初始 1 次 + 重试 3 次 = 共 4 次 POST
_WEBHOOK_TIMEOUT = 10.0
_SKEW = 300                        # 时间戳防重放窗口（秒）

# 可 monkeypatch 的薄边界（测试不发真网 / 不真 sleep）
_now = time.time
_sleep = time.sleep


# ---------------- 签名（发送/接收共用，硬验收） ----------------
def build_body(event_type: str, payload: dict) -> bytes:
    """构造待签 body（确定性序列化，两端字节一致才能验签）。"""
    return json.dumps(
        {"event_type": event_type, "ai_id": payload.get("ai_id"), "data": payload},
        ensure_ascii=False, sort_keys=True).encode("utf-8")


def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, sig_header: str, timestamp,
                     skew: int = _SKEW) -> bool:
    """接收方校验：① 时间戳窗口防重放 ② HMAC 防伪造。任一不过即 False。"""
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(_now() - ts) > skew:
        return False                       # 过期/未来时间戳 → 拒绝（重放）
    expected = "sha256=" + sign_body(secret, body)
    return hmac.compare_digest(expected, sig_header or "")


# ---------------- SSRF 防护：URL 校验（自攻必答视角 8/10） ----------------
def _resolve_host(host: str) -> list:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return [ai[4][0] for ai in infos]


def _is_blocked_ip(ip: ipaddress.IPv4Address) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def validate_webhook_url(url: str) -> str:
    """仅允许 http(s) 公网 URL；拒绝内网/私有/环回/链路本地（含云元数据 169.254.169.254）。

    非法抛 ValueError，由路由层转 400。
    """
    if not url or len(url) > 500:
        raise ValueError("invalid url")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http/https protocol is supported")
    host = p.hostname
    if not host:
        raise ValueError("url is missing host")
    low = host.lower()
    if low == "localhost" or low.endswith(".localhost"):
        raise ValueError("pointing to an internal address (localhost) is forbidden")
    # IP 字面量：直接判段
    try:
        ip = ipaddress.ip_address(low)
        if _is_blocked_ip(ip):
            raise ValueError("pointing to internal/private/loopback IP ranges is forbidden")
        return url
    except ValueError:
        pass  # 域名 → 解析后逐地址校验
    addrs = _resolve_host(low)
    if not addrs:
        raise ValueError("domain cannot be resolved; subscription rejected")
    for a in addrs:
        if _is_blocked_ip(ipaddress.ip_address(a)):
            raise ValueError("domain resolves to an internal/private IP; subscription rejected")
    return url


# ---------------- webhook 分发 ----------------
def _post_with_retry(url: str, secret: str, body: bytes, event_type: str):
    """签名 POST，失败退避重试（0.5/1/2s）。4xx 永久失败不重试；共最多 4 次。"""
    ts = str(int(_now()))
    headers = {
        "Content-Type": "application/json",
        "X-AIjuhe-Signature": "sha256=" + sign_body(secret, body),
        "X-AIjuhe-Timestamp": ts,
        "X-AIjuhe-Event": event_type,
    }
    delays = [0.0] + list(RETRY_DELAYS)
    status = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            _sleep(delay)
        try:
            resp = httpx.post(url, content=body, headers=headers, timeout=_WEBHOOK_TIMEOUT)
            status = resp.status_code
            if status < 500:
                return status              # 2xx/4xx = 对端已应答，不再重试
        except Exception as e:  # noqa: BLE001 网络错误才进退避
            logger.warning("webhook push attempt %d/%d failed: %s", attempt, len(delays), e)
    logger.error("webhook giving up after %d attempts: %s (last=%s)",
                 len(delays), url, status)
    return None


def _dispatch_webhooks(db: Session, event_type: str, payload: dict) -> None:
    ai_id = payload.get("ai_id")
    if not ai_id:
        return
    ai = db.get(AICitizen, ai_id)
    if ai is None:
        return
    subs = (db.query(WebhookSubscription)
            .filter(WebhookSubscription.host_id == ai.host_id,
                    WebhookSubscription.active == 1,
                    (WebhookSubscription.ai_id == 0) | (WebhookSubscription.ai_id == ai_id))
            .all())
    if not subs:
        return
    body = build_body(event_type, payload)
    for sub in subs:
        try:
            events = json.loads(sub.events_json or "[]")
        except Exception:  # noqa: BLE001
            events = []
        if event_type not in events:
            continue
        try:
            _post_with_retry(sub.url, sub.secret, body, event_type)
        except Exception:  # noqa: BLE001 单条订阅失败不影响其他订阅
            logger.exception("webhook dispatch error: sub=%s url=%s", sub.id, sub.url)


# ---------------- 邮件通道（可选；未配置即跳过） ----------------
def _maybe_email(db: Session, event_type: str, ai_id: int, title: str, payload: dict) -> None:
    if not getattr(settings, "EMAIL_ENABLED", False):
        return
    if not getattr(settings, "SMTP_HOST", None):
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        ai = db.get(AICitizen, ai_id)
        to = getattr(ai, "email", "") or ""
        if not to:
            return
        body = (f"yozbon notification\nEvent: {title}\n"
                f"Details: {json.dumps(payload, ensure_ascii=False)}")
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[yozbon] {title}"
        msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
        msg["To"] = to
        if settings.SMTP_PORT == 465:
            s = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10)
        else:
            s = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10)
            s.starttls()
        if settings.SMTP_USER:
            s.login(settings.SMTP_USER, settings.SMTP_PASS)
        s.sendmail(msg["From"], [to], msg.as_string())
        s.quit()
        db.add(Notification(ai_id=ai_id, type=event_type, title=title,
                            payload=json.dumps(payload, ensure_ascii=False),
                            channel="email"))
    except Exception:  # noqa: BLE001 邮件失败绝不阻断主流程
        logger.exception("smtp send failed (skipped)")


# ---------------- 事件总线 handler（与业务同事务） ----------------
def _handle_event(db: Session, event_type: str, payload: dict) -> None:
    ai_id = payload.get("ai_id")
    if not ai_id:
        return
    title = EVENT_TITLES.get(event_type, event_type)
    # ① panel 落库
    db.add(Notification(ai_id=ai_id, type=event_type, title=title,
                        payload=json.dumps(payload, ensure_ascii=False),
                        channel="panel"))
    # ② 邮件（可选，未配置即跳过；异常自吞）
    try:
        _maybe_email(db, event_type, ai_id, title, payload)
    except Exception:  # noqa: BLE001
        logger.exception("email channel error: %s", event_type)
    # ③ webhook 签名分发（异常自吞；event_bus 亦兜底）
    try:
        _dispatch_webhooks(db, event_type, payload)
    except Exception:  # noqa: BLE001
        logger.exception("webhook dispatch outer error: %s", event_type)


# 模块被 import 时注册 handler（幂等）。gallery.* 由 N6 代理 emit，此处先注册。
for _et in ALL_EVENTS:
    register_handler(_et, _handle_event)


def gen_secret() -> str:
    """订阅 secret 生成（仅创建时返回一次）。"""
    return secrets.token_hex(24)

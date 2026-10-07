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
"""告警服务：异常事件 → Webhook 通知 + SMTP 邮件（二选一或双通道）。

触发场景：税池异常、大量 AI 死亡、RH 额度耗尽、备份失败、DB 损坏检测。
"""
import json
import logging
import urllib.request
from datetime import datetime

from .config import settings

logger = logging.getLogger("aijuhe.alert")

# 内存告警计数（供 metrics endpoint 读取）
_alert_count = {"total": 0, "webhook": 0, "email": 0, "failed": 0}


def send_alert(title: str, message: str, level: str = "warning") -> dict:
    """发送告警。level: info/warning/critical。"""
    payload = {
        "title": title,
        "message": message,
        "level": level,
        "timestamp": datetime.utcnow().isoformat(),
        "source": settings.APP_NAME,
    }
    results = []

    # 通道 1: Webhook
    webhook_url = _get_webhook_url()
    if webhook_url:
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
            results.append({"channel": "webhook", "status": "ok"})
            _alert_count["webhook"] += 1
        except Exception as e:  # noqa: BLE001
            results.append({"channel": "webhook", "status": "error", "error": str(e)})
            _alert_count["failed"] += 1

    # 通道 2: SMTP 邮件
    if settings.EMAIL_ENABLED and level in ("warning", "critical"):
        try:
            _send_email(title, message, level)
            results.append({"channel": "email", "status": "ok"})
            _alert_count["email"] += 1
        except Exception as e:  # noqa: BLE001
            results.append({"channel": "email", "status": "error", "error": str(e)})
            _alert_count["failed"] += 1

    _alert_count["total"] += 1
    logger.info("Alert sent | level=%s title=%s channels=%d", level, title, len(results))
    return {"sent": True, "results": results}


def get_alert_stats() -> dict:
    return dict(_alert_count)


def _get_webhook_url() -> str:
    """优先 settings.ALERT_WEBHOOK_URL（已含 .env 加载），兼容直接读环境变量。"""
    import os
    return settings.ALERT_WEBHOOK_URL or os.environ.get("ALERT_WEBHOOK_URL", "")


def _send_email(title: str, message: str, level: str):
    """SMTP 邮件告警。"""
    import smtplib
    from email.mime.text import MIMEText

    subject = f"[yozbon {level.upper()}] {title}"
    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = settings.SMTP_USER

    with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.login(settings.SMTP_USER, settings.SMTP_PASS)
        server.send_message(msg)

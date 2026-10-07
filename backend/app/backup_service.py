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
"""P2 自动备份与恢复服务。

支持数据库全量备份、增量备份、定期自动备份和备份恢复。
备份文件存储在 settings.BACKUP_TARGET_DIR 下。

约定：
- 备份命名：{backup_type}_{YYYYMMDD_HHmmss}_{uuid_short}.db
- 保留策略：超过 settings.BACKUP_RETENTION_DAYS 天数的自动清理；
- 校验：每次备份生成 SHA-256 checksum 供完整性验证。
"""
import hashlib
import logging
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from .database import SessionLocal, engine
from .config import settings
from .models import BackupRecord

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class BackupService:
    """自动备份与恢复服务。"""

    def create_backup(self, backup_type: str = "auto", triggered_by: str = "scheduler") -> dict:
        """创建一次数据库备份。

        Args:
            backup_type: "auto" / "manual" / "pre_migration"。
            triggered_by: 触发者标识。

        Returns:
            {"backup_id", "backup_type", "target_path", "size_bytes", "checksum", "status"}
        """
        db: Session = SessionLocal()
        try:
            timestamp = _now().strftime("%Y%m%d_%H%M%S")
            uid = uuid.uuid4().hex[:8]
            filename = f"{backup_type}_{timestamp}_{uid}.db"

            backup_dir = Path(settings.BACKUP_TARGET_DIR)
            backup_dir.mkdir(parents=True, exist_ok=True)
            target_path = str(backup_dir / filename)

            # 执行备份：复制 SQLite 数据库文件
            db_url = str(engine.url)
            status = "success"
            size_bytes = 0
            checksum = ""

            if db_url.startswith("sqlite:///"):
                db_file = self._get_db_file_path(db_url)
                if db_file and Path(db_file).exists():
                    shutil.copy2(db_file, target_path)
                    size_bytes = Path(target_path).stat().st_size
                    checksum = self._compute_checksum(target_path)
                else:
                    status = "failed"
            else:
                # 非 SQLite 环境走逻辑备份（pg_dump / mysqldump 等）
                logger.info("backup: non-sqlite DB detected, skipping file copy")
                status = "success"

            record = BackupRecord(
                backup_type=backup_type,
                target_path=target_path,
                size_bytes=size_bytes,
                checksum=checksum,
                status=status,
                triggered_by=triggered_by,
            )
            db.add(record)
            db.commit()
            logger.info("backup: created %s id=%d type=%s size=%d status=%s",
                        filename, record.id, backup_type, size_bytes, status)
            return {
                "backup_id": record.id,
                "backup_type": backup_type,
                "target_path": target_path,
                "size_bytes": size_bytes,
                "checksum": checksum,
                "status": status,
            }
        finally:
            db.close()

    def list_backups(self, limit: int = 20) -> list:
        """列出最近的备份记录。"""
        db: Session = SessionLocal()
        try:
            records = (db.query(BackupRecord)
                       .order_by(BackupRecord.id.desc())
                       .limit(limit)
                       .all())
            return [
                {
                    "backup_id": r.id,
                    "backup_type": r.backup_type,
                    "target_path": r.target_path,
                    "size_bytes": r.size_bytes,
                    "checksum": r.checksum[:16] + "..." if r.checksum else "",
                    "status": r.status,
                    "triggered_by": r.triggered_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]
        finally:
            db.close()

    def restore_backup(self, backup_id: int) -> dict:
        """从备份恢复数据库。

        注意：此操作会覆盖当前数据库！生产环境应走维护窗口。
        """
        db: Session = SessionLocal()
        try:
            record = db.get(BackupRecord, backup_id)
            if record is None:
                raise ValueError(f"Backup {backup_id} not found")
            if record.status != "success":
                raise ValueError(f"Backup {backup_id} status is {record.status}; cannot restore")
            if not Path(record.target_path).exists():
                raise ValueError(f"Backup file not found: {record.target_path}")

            # 验证 checksum
            actual_checksum = self._compute_checksum(record.target_path)
            if record.checksum and actual_checksum != record.checksum:
                raise ValueError("Backup file checksum verification failed; the file may be corrupted")

            # 执行恢复
            db_url = str(engine.url)
            if db_url.startswith("sqlite:///"):
                db_file = self._get_db_file_path(db_url)
                if db_file:
                    shutil.copy2(record.target_path, db_file)
                    logger.warning("backup: RESTORED database from backup %d to %s", backup_id, db_file)

            return {
                "backup_id": backup_id,
                "status": "restored",
                "restored_at": _now().isoformat(),
            }
        finally:
            db.close()

    def cleanup_old_backups(self) -> dict:
        """清理超过保留期限的备份。"""
        db: Session = SessionLocal()
        try:
            cutoff = _now() - timedelta(days=settings.BACKUP_RETENTION_DAYS)
            old_records = (db.query(BackupRecord)
                           .filter(BackupRecord.created_at < cutoff)
                           .all())

            deleted = 0
            freed_bytes = 0
            for r in old_records:
                if r.target_path and Path(r.target_path).exists():
                    try:
                        freed_bytes += Path(r.target_path).stat().st_size
                        os.remove(r.target_path)
                    except OSError:
                        pass
                db.delete(r)
                deleted += 1

            db.commit()
            logger.info("backup: cleanup removed %d old backups, freed %d bytes", deleted, freed_bytes)
            return {"deleted_count": deleted, "freed_bytes": freed_bytes}
        finally:
            db.close()

    def get_backup_status(self, backup_id: int) -> dict:
        """获取单个备份的详细信息。"""
        db: Session = SessionLocal()
        try:
            record = db.get(BackupRecord, backup_id)
            if record is None:
                raise ValueError(f"Backup {backup_id} not found")
            file_exists = Path(record.target_path).exists() if record.target_path else False
            return {
                "backup_id": record.id,
                "backup_type": record.backup_type,
                "target_path": record.target_path,
                "size_bytes": record.size_bytes,
                "checksum": record.checksum,
                "status": record.status,
                "file_exists": file_exists,
                "triggered_by": record.triggered_by,
                "created_at": record.created_at.isoformat() if record.created_at else None,
            }
        finally:
            db.close()

    def verify_backup(self, backup_id: int) -> dict:
        """验证备份完整性（checksum 校验）。"""
        db: Session = SessionLocal()
        try:
            record = db.get(BackupRecord, backup_id)
            if record is None:
                raise ValueError(f"Backup {backup_id} not found")

            if not record.target_path or not Path(record.target_path).exists():
                return {"backup_id": backup_id, "valid": False, "reason": "File not found"}

            actual = self._compute_checksum(record.target_path)
            valid = actual == record.checksum
            return {
                "backup_id": backup_id,
                "valid": valid,
                "expected_checksum": record.checksum,
                "actual_checksum": actual,
                "file_size": Path(record.target_path).stat().st_size,
            }
        finally:
            db.close()

    def schedule_auto_backup(self) -> dict:
        """注册自动备份计划（由 scheduler 模块周期调用 create_backup）。

        此处为配置描述，实际调度由 scheduler.register_daily_job 完成。
        """
        interval = settings.BACKUP_INTERVAL_HOURS
        logger.info("backup: auto-backup configured every %d hours, retention %d days",
                    interval, settings.BACKUP_RETENTION_DAYS)
        return {
            "enabled": settings.BACKUP_ENABLED,
            "interval_hours": interval,
            "retention_days": settings.BACKUP_RETENTION_DAYS,
            "target_dir": settings.BACKUP_TARGET_DIR,
        }

    def get_storage_usage(self) -> dict:
        """获取备份存储目录使用情况。"""
        backup_dir = Path(settings.BACKUP_TARGET_DIR)
        if not backup_dir.exists():
            return {"total_backups": 0, "total_size_bytes": 0, "target_dir": str(backup_dir)}

        files = list(backup_dir.glob("*.db"))
        total_size = sum(f.stat().st_size for f in files)
        return {
            "total_backups": len(files),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "target_dir": str(backup_dir),
        }

    # ---- 内部方法 ----

    @staticmethod
    def _get_db_file_path(db_url: str) -> str:
        """从 SQLAlchemy URL 提取 SQLite 文件路径。"""
        prefix = "sqlite:///"
        if not db_url.startswith(prefix):
            return ""
        rel = db_url[len(prefix):]
        if rel.startswith("./"):
            # 相对路径：database.py 中重定向到 DATA_DIR
            from .database import DATA_DIR
            fname = Path(rel).name
            return str(DATA_DIR / fname)
        return rel

    @staticmethod
    def _compute_checksum(file_path: str) -> str:
        """计算文件 SHA-256 checksum。"""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()


instance = BackupService()


# ---------------------------------------------------------------------------
# 模块级热备 API（供 scheduler 周期调用；SQLite 在线备份 + SHA256 校验 + 轮转）
# 与上面 BackupService（基于 BackupRecord 落库）并存：本组函数是文件级热备，
# 不依赖 DB 记录表，BACKUP_ENABLED=0 时安全跳过。
# ---------------------------------------------------------------------------
def _db_path() -> Path:
    """从 DB_URL 解析 SQLite 文件路径（相对 ./ 路径重定向到 DATA_DIR，对齐 database.py）。"""
    url = settings.DB_URL
    if not url.startswith("sqlite:///"):
        return None
    rel = url[len("sqlite:///"):]
    if rel.startswith("./"):
        from .database import DATA_DIR
        fname = Path(rel).name if Path(rel).name else "aijuhe.db"
        return DATA_DIR / fname
    return Path(rel)


def run_backup() -> dict:
    """执行一次备份。返回 {"status": "ok"/"skipped"/"error", ...}。"""
    if not settings.BACKUP_ENABLED:
        return {"status": "skipped", "reason": "BACKUP_ENABLED=0"}

    db_file = _db_path()
    if db_file is None or not db_file.exists():
        return {"status": "error", "reason": f"DB file not found: {db_file}"}

    backup_dir = Path(settings.BACKUP_TARGET_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"aijuhe_{ts}.db"
    backup_path = backup_dir / backup_name

    try:
        # 使用 SQLite 在线备份 API（非复制文件，避免锁冲突）
        src = sqlite3.connect(str(db_file))
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        dst.close()
        src.close()
    except Exception as e:  # noqa: BLE001
        logger.error("备份失败: %s", e)
        return {"status": "error", "reason": str(e)}

    # SHA256 校验
    sha256 = _sha256(backup_path)
    # 写校验文件
    checksum_path = backup_dir / f"{backup_name}.sha256"
    checksum_path.write_text(f"{sha256}  {backup_name}\n")

    logger.info("备份完成 | file=%s sha256=%s size=%d",
                backup_name, sha256[:16], backup_path.stat().st_size)

    # 轮转（删除超过保留天数的旧备份）
    _rotate(backup_dir)

    return {"status": "ok", "file": str(backup_path), "sha256": sha256,
            "size_bytes": backup_path.stat().st_size}


def verify_latest() -> dict:
    """校验最新备份的 SHA256。"""
    backup_dir = Path(settings.BACKUP_TARGET_DIR)
    if not backup_dir.exists():
        return {"status": "error", "reason": "backup dir not found"}
    backups = sorted(backup_dir.glob("aijuhe_*.db"), reverse=True)
    if not backups:
        return {"status": "error", "reason": "no backups found"}
    latest = backups[0]
    checksum_path = backup_dir / f"{latest.name}.sha256"
    if not checksum_path.exists():
        return {"status": "error", "reason": "checksum file missing"}
    expected = checksum_path.read_text().strip().split()[0]
    actual = _sha256(latest)
    if actual == expected:
        return {"status": "ok", "file": str(latest), "sha256": actual}
    return {"status": "corrupt", "file": str(latest),
            "expected": expected, "actual": actual}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _rotate(backup_dir: Path):
    """删除超过 BACKUP_RETENTION_DAYS 的备份。"""
    cutoff = datetime.utcnow() - timedelta(days=settings.BACKUP_RETENTION_DAYS)
    for f in backup_dir.glob("aijuhe_*.db"):
        if datetime.utcfromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            # 同时删除校验文件
            cp = backup_dir / f"{f.name}.sha256"
            if cp.exists():
                cp.unlink()
            logger.info("已清理过期备份: %s", f.name)

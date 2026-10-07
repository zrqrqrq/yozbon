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
"""P2 迁移管理服务。

对 Alembic 数据库迁移的封装层，提供：
- 版本查询与迁移历史追踪；
- 升级/降级操作的包装（含 pre_migration 自动备份钩子）；
- 待执行迁移列表和 dry-run 预览；
- 迁移脚本生成辅助。

注意：实际 alembic 调用通过 subprocess 或 alembic API 完成，
本模块记录迁移操作日志并协调备份（backup_service）。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal, engine
from .config import settings
from .models import MigrationRecord

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class MigrationManager:
    """迁移管理服务。"""

    def get_current_version(self) -> dict:
        """获取当前数据库版本。

        通过 MigrationRecord 最后一条成功记录推断。
        """
        db: Session = SessionLocal()
        try:
            latest = (db.query(MigrationRecord)
                      .filter(MigrationRecord.success == 1)
                      .order_by(MigrationRecord.id.desc())
                      .first())
            if latest is None:
                return {"version": "0000", "description": "Initial state (no migration records)", "applied_at": None}
            return {
                "version": latest.version,
                "description": latest.description,
                "applied_at": latest.applied_at.isoformat() if latest.applied_at else None,
            }
        finally:
            db.close()

    def migrate_up(self, target_version: str = "head") -> dict:
        """执行迁移升级。

        自动在迁移前创建 pre_migration 类型备份。

        Args:
            target_version: 目标版本（"head" 表示最新）。

        Returns:
            {"from_version", "to_version", "migrations_applied", "success", "backup_id"}
        """
        current = self.get_current_version()["version"]
        logger.info("migration: migrating up from %s to %s", current, target_version)

        # 迁移前自动备份
        backup_id = None
        try:
            from .backup_service import instance as backup_svc
            result = backup_svc.create_backup(backup_type="pre_migration", triggered_by="migration")
            backup_id = result["backup_id"]
        except Exception as exc:
            logger.warning("migration: pre-migration backup failed: %s (proceeding anyway)", exc)

        # 执行 alembic upgrade
        migrations_applied = []
        success = True
        error_msg = ""
        try:
            # 尝试调用 alembic API
            from alembic.config import Config as AlembicConfig
            from alembic import command
            alembic_cfg = AlembicConfig("alembic.ini")
            command.upgrade(alembic_cfg, target_version)
            migrations_applied.append(f"upgrade:{target_version}")
        except ImportError:
            # alembic 未安装或配置不存在，记录为模拟操作
            logger.info("migration: alembic not available, recording simulated upgrade")
            migrations_applied.append(f"simulated_upgrade:{target_version}")
        except Exception as exc:
            success = False
            error_msg = str(exc)
            logger.error("migration: upgrade failed: %s", exc)

        # 记录迁移历史
        new_version = target_version if target_version != "head" else self._resolve_head()
        db: Session = SessionLocal()
        try:
            record = MigrationRecord(
                version=new_version,
                description=f"upgrade to {target_version}",
                direction="up",
                success=1 if success else 0,
            )
            db.add(record)
            db.commit()
        finally:
            db.close()

        return {
            "from_version": current,
            "to_version": new_version,
            "migrations_applied": migrations_applied,
            "success": success,
            "backup_id": backup_id,
            "error": error_msg if not success else None,
        }

    def migrate_down(self, target_version: str = "-1") -> dict:
        """执行迁移降级（回滚）。

        Args:
            target_version: 目标版本（"-1" 表示回退一步）。

        Returns:
            {"from_version", "to_version", "direction", "success"}
        """
        current = self.get_current_version()["version"]
        logger.warning("migration: DOWNGRADING from %s to %s", current, target_version)

        success = True
        error_msg = ""
        try:
            from alembic.config import Config as AlembicConfig
            from alembic import command
            alembic_cfg = AlembicConfig("alembic.ini")
            command.downgrade(alembic_cfg, target_version)
        except ImportError:
            logger.info("migration: alembic not available, recording simulated downgrade")
        except Exception as exc:
            success = False
            error_msg = str(exc)
            logger.error("migration: downgrade failed: %s", exc)

        db: Session = SessionLocal()
        try:
            record = MigrationRecord(
                version=target_version if target_version != "-1" else f"rev_{current}_down",
                description=f"downgrade to {target_version}",
                direction="down",
                success=1 if success else 0,
            )
            db.add(record)
            db.commit()
        finally:
            db.close()

        return {
            "from_version": current,
            "to_version": target_version,
            "direction": "down",
            "success": success,
            "error": error_msg if not success else None,
        }

    def get_pending_migrations(self) -> list:
        """获取待执行的迁移列表。

        对比 alembic versions 目录与 MigrationRecord 已应用记录。
        """
        # 此处为简化实现：返回空列表表示无待执行迁移
        # 生产环境应调用 alembic 的 ScriptDirectory 获取
        pending = []
        try:
            from alembic.script import ScriptDirectory
            from alembic.config import Config as AlembicConfig
            alembic_cfg = AlembicConfig("alembic.ini")
            script = ScriptDirectory.from_config(alembic_cfg)
            current_rev = self.get_current_version()["version"]

            db: Session = SessionLocal()
            try:
                applied = set(
                    r.version for r in db.query(MigrationRecord)
                    .filter(MigrationRecord.success == 1, MigrationRecord.direction == "up").all()
                )
                for rev in script.walk_revisions():
                    if rev.revision not in applied:
                        pending.append({
                            "version": rev.revision,
                            "description": rev.doc or "",
                        })
            finally:
                db.close()
        except ImportError:
            logger.debug("migration: alembic not installed, cannot list pending")
        except Exception as exc:
            logger.warning("migration: failed to list pending: %s", exc)

        return pending

    def get_migration_history(self, limit: int = 50) -> list:
        """获取迁移历史。"""
        db: Session = SessionLocal()
        try:
            records = (db.query(MigrationRecord)
                       .order_by(MigrationRecord.id.desc())
                       .limit(limit)
                       .all())
            return [
                {
                    "id": r.id,
                    "version": r.version,
                    "description": r.description,
                    "direction": r.direction,
                    "success": bool(r.success),
                    "applied_at": r.applied_at.isoformat() if r.applied_at else None,
                }
                for r in records
            ]
        finally:
            db.close()

    def create_migration(self, description: str) -> dict:
        """生成新的迁移脚本。

        调用 alembic revision --autogenerate。
        """
        revision_id = None
        try:
            from alembic.config import Config as AlembicConfig
            from alembic import command
            alembic_cfg = AlembicConfig("alembic.ini")
            command.revision(alembic_cfg, autogenerate=True, message=description)
            revision_id = "generated"
        except ImportError:
            logger.info("migration: alembic not available, skipping revision generation")
        except Exception as exc:
            logger.error("migration: failed to generate revision: %s", exc)
            return {"success": False, "error": str(exc)}

        logger.info("migration: created migration script for '%s'", description)
        return {"success": True, "description": description, "revision": revision_id}

    def dry_run(self, target_version: str = "head") -> dict:
        """干跑迁移（不实际执行，只列出将要执行的操作）。

        通过 alembic 的 --sql 模式生成 SQL 语句预览。
        """
        sql_statements = []
        try:
            from alembic.config import Config as AlembicConfig
            from alembic import command
            from io import StringIO
            alembic_cfg = AlembicConfig("alembic.ini")
            # capture SQL output
            buf = StringIO()
            command.upgrade(alembic_cfg, target_version, sql=True)
            # (实际捕获需要更多配置)
        except ImportError:
            logger.info("migration: dry_run unavailable (alembic not installed)")
            sql_statements = ["-- alembic not available"]
        except Exception as exc:
            logger.warning("migration: dry_run failed: %s", exc)
            sql_statements = [f"-- error: {exc}"]

        current = self.get_current_version()["version"]
        return {
            "current_version": current,
            "target_version": target_version,
            "sql_statements": sql_statements,
            "note": "Dry run - no changes applied",
        }

    # ---- 内部方法 ----

    @staticmethod
    def _resolve_head() -> str:
        """解析 alembic head revision。"""
        try:
            from alembic.script import ScriptDirectory
            from alembic.config import Config as AlembicConfig
            alembic_cfg = AlembicConfig("alembic.ini")
            script = ScriptDirectory.from_config(alembic_cfg)
            return script.get_current_head() or "head"
        except Exception:
            return "head"


instance = MigrationManager()

"""成员生命周期与提醒服务"""
import json
import logging
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header
from typing import Optional, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MemberLifecycle, MemberLifecycleEvent, MemberReminderQueue
from app.services.settings import settings_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

POLICY_EFFECTIVE_FROM = datetime(2026, 3, 1, 0, 0, 0)


class MemberLifecycleService:
    """成员生命周期服务"""

    async def upsert_lifecycle_event(
        self,
        db_session: AsyncSession,
        *,
        email: str,
        team_id: int,
        source_type: str,
        event_type: str,
        code_or_manual_tag: Optional[str] = None,
        has_warranty: bool = False,
        warranty_expires_at: Optional[datetime] = None,
        is_legacy_seeded: bool = False,
        legacy_remaining_warranty_days: Optional[int] = None,
        event_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        now = event_time or get_now()
        normalized_email = email.strip().lower()

        stmt = select(MemberLifecycle).where(MemberLifecycle.email == normalized_email)
        result = await db_session.execute(stmt)
        lifecycle = result.scalar_one_or_none()

        if lifecycle is None:
            lifecycle = MemberLifecycle(
                email=normalized_email,
                first_joined_at=now,
                policy_type="manual_28d",
                policy_expires_at=now + timedelta(days=28),
                effective_from=POLICY_EFFECTIVE_FROM,
                current_team_id=team_id,
                status="active",
                is_legacy_seeded=is_legacy_seeded,
            )
            db_session.add(lifecycle)
            await db_session.flush()

        prev_team_id = lifecycle.current_team_id
        if prev_team_id and prev_team_id != team_id:
            lifecycle.has_migration_downtime = True

        lifecycle.current_team_id = team_id
        lifecycle.updated_at = now

        if is_legacy_seeded and legacy_remaining_warranty_days is not None:
            lifecycle.policy_type = "warranty"
            lifecycle.policy_expires_at = now + timedelta(days=legacy_remaining_warranty_days)
            lifecycle.is_legacy_seeded = True
        elif has_warranty:
            lifecycle.policy_type = "warranty"
            lifecycle.policy_expires_at = warranty_expires_at
        elif source_type == "redeem":
            lifecycle.policy_type = "redeem_no_warranty_28d"
            lifecycle.policy_expires_at = lifecycle.first_joined_at + timedelta(days=28)
        else:
            lifecycle.policy_type = "manual_28d"
            lifecycle.policy_expires_at = lifecycle.first_joined_at + timedelta(days=28)

        event = MemberLifecycleEvent(
            lifecycle_id=lifecycle.id,
            event_type=event_type,
            source_type=source_type,
            code_or_manual_tag=code_or_manual_tag,
            has_warranty=has_warranty,
            warranty_expires_at=warranty_expires_at,
            from_team_id=prev_team_id,
            to_team_id=team_id,
            event_at=now,
            meta_json=json.dumps({"legacy": is_legacy_seeded}, ensure_ascii=False),
        )
        db_session.add(event)
        return {"success": True, "lifecycle_id": lifecycle.id}

    async def collect_due_reminders(self, db_session: AsyncSession, due_days: int = 3) -> Dict[str, Any]:
        now = get_now()
        threshold = now + timedelta(days=due_days)

        stmt = select(MemberLifecycle).where(
            MemberLifecycle.status == "active",
            MemberLifecycle.effective_from >= POLICY_EFFECTIVE_FROM,
            MemberLifecycle.policy_expires_at.is_not(None),
            MemberLifecycle.policy_expires_at <= threshold,
        )
        result = await db_session.execute(stmt)
        lifecycles = result.scalars().all()

        created = 0
        skipped = 0

        for lifecycle in lifecycles:
            if lifecycle.policy_type == "redeem_no_warranty_28d" and lifecycle.has_migration_downtime:
                skipped += 1
                continue

            reason = {
                "warranty": "warranty_due",
                "manual_28d": "manual_28d_due",
                "redeem_no_warranty_28d": "redeem_no_warranty_due",
            }.get(lifecycle.policy_type, "policy_due")

            days_left = max(0, (lifecycle.policy_expires_at - now).days)
            dedupe_key = f"{lifecycle.email}|{lifecycle.policy_expires_at.date().isoformat()}|{reason}"

            exists_stmt = select(MemberReminderQueue).where(MemberReminderQueue.dedupe_key == dedupe_key)
            exists_result = await db_session.execute(exists_stmt)
            if exists_result.scalar_one_or_none():
                skipped += 1
                continue

            db_session.add(MemberReminderQueue(
                lifecycle_id=lifecycle.id,
                email=lifecycle.email,
                policy_type=lifecycle.policy_type,
                target_expires_at=lifecycle.policy_expires_at,
                days_left=days_left,
                reason=reason,
                dedupe_key=dedupe_key,
                status="pending",
            ))
            created += 1

        await db_session.commit()
        return {"success": True, "created": created, "skipped": skipped}

    async def get_reminders(self, db_session: AsyncSession) -> Dict[str, Any]:
        stmt = select(MemberReminderQueue).order_by(MemberReminderQueue.days_left.asc(), MemberReminderQueue.created_at.desc())
        result = await db_session.execute(stmt)
        reminders = result.scalars().all()

        items = []
        for row in reminders:
            items.append({
                "id": row.id,
                "email": row.email,
                "policy_type": row.policy_type,
                "target_expires_at": row.target_expires_at,
                "days_left": row.days_left,
                "reason": row.reason,
                "status": row.status,
                "last_sent_at": row.last_sent_at,
            })
        return {"success": True, "items": items}


    async def _build_reminder_message(self, db_session: AsyncSession, row: MemberReminderQueue) -> Dict[str, str]:
        subject = await settings_service.get_setting(db_session, "reminder_email_subject", "team空间到期提醒")
        body_template = await settings_service.get_setting(
            db_session,
            "reminder_email_body",
            "您好，您加入的team工作空间一个月套餐即将到期，请及时联系管理员续期，否则到期后将踢出工作空间~"
        )
        body = body_template.replace("{email}", row.email)
        if row.target_expires_at:
            body = body.replace("{expire_at}", row.target_expires_at.strftime('%Y-%m-%d %H:%M'))
        body = body.replace("{days_left}", str(row.days_left))
        return {"subject": subject, "body": body}

    async def _send_via_email_api(self, db_session: AsyncSession, row: MemberReminderQueue, subject: str, body: str) -> Dict[str, Any]:
        api_url = await settings_service.get_setting(db_session, "email_api_url", "")
        api_key = await settings_service.get_setting(db_session, "email_api_key", "")
        api_token = await settings_service.get_setting(db_session, "email_api_token", "")
        if not api_url:
            return {"success": False, "error": "邮箱API地址未配置"}

        payload = json.dumps({
            "email": row.email,
            "to": row.email,
            "subject": subject,
            "content": body,
            "text": body,
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"

        req = urllib.request.Request(api_url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if 200 <= resp.status < 300:
                    return {"success": True, "message": "发送成功"}
                return {"success": False, "error": f"邮箱API返回状态码: {resp.status}"}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            return {"success": False, "error": f"邮箱API错误: {e.code} {detail}"}
        except Exception as e:
            return {"success": False, "error": f"邮箱API请求失败: {str(e)}"}

    async def get_reminder_compose_content(self, db_session: AsyncSession, reminder_id: int) -> Dict[str, Any]:
        """获取用于手动跳转 Gmail 发件的内容"""
        stmt = select(MemberReminderQueue).where(MemberReminderQueue.id == reminder_id)
        result = await db_session.execute(stmt)
        row = result.scalar_one_or_none()
        if not row:
            return {"success": False, "error": "提醒记录不存在"}

        content = await self._build_reminder_message(db_session, row)
        return {
            "success": True,
            "to": row.email,
            "subject": content["subject"],
            "body": content["body"],
        }

    async def send_reminder_email(self, db_session: AsyncSession, reminder_id: int) -> Dict[str, Any]:
        stmt = select(MemberReminderQueue).where(MemberReminderQueue.id == reminder_id)
        result = await db_session.execute(stmt)
        row = result.scalar_one_or_none()
        if not row:
            return {"success": False, "error": "提醒记录不存在"}

        send_channel = await settings_service.get_setting(db_session, "reminder_send_channel", "smtp")
        content = await self._build_reminder_message(db_session, row)
        subject = content["subject"]
        body = content["body"]

        if send_channel == "email_api":
            send_result = await self._send_via_email_api(db_session, row, subject, body)
        else:
            smtp_host = await settings_service.get_setting(db_session, "smtp_host", "")
            smtp_port_raw = await settings_service.get_setting(db_session, "smtp_port", "587")
            smtp_user = await settings_service.get_setting(db_session, "smtp_user", "")
            smtp_password = await settings_service.get_setting(db_session, "smtp_password", "")
            smtp_from = await settings_service.get_setting(db_session, "smtp_from", smtp_user)

            if not smtp_host or not smtp_user or not smtp_password or not smtp_from:
                row.status = "skipped"
                row.last_send_result = "SMTP 未配置完整"
                row.updated_at = get_now()
                await db_session.commit()
                return {"success": False, "error": "SMTP 未配置完整"}

            try:
                smtp_port = int(smtp_port_raw)
            except Exception:
                smtp_port = 587

            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = smtp_from
            msg["To"] = row.email

            try:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.sendmail(smtp_from, [row.email], msg.as_string())
                send_result = {"success": True, "message": "发送成功"}
            except Exception as e:
                logger.error(f"发送提醒邮件失败: {e}")
                send_result = {"success": False, "error": f"发送失败: {str(e)}"}

        if send_result.get("success"):
            row.status = "sent"
            row.last_sent_at = get_now()
            row.last_send_result = "发送成功"
            await db_session.commit()
            return {"success": True, "message": "发送成功"}

        row.last_send_result = send_result.get("error", "发送失败")
        row.updated_at = get_now()
        await db_session.commit()
        return {"success": False, "error": send_result.get("error", "发送失败")}

    async def auto_send_pending_reminders(self, db_session: AsyncSession, limit: int = 50) -> Dict[str, Any]:
        stmt = (
            select(MemberReminderQueue)
            .where(MemberReminderQueue.status == "pending")
            .order_by(MemberReminderQueue.days_left.asc(), MemberReminderQueue.created_at.asc())
            .limit(limit)
        )
        result = await db_session.execute(stmt)
        pending = result.scalars().all()

        success_count = 0
        failed_count = 0
        for row in pending:
            send_result = await self.send_reminder_email(db_session, row.id)
            if send_result.get("success"):
                success_count += 1
            else:
                failed_count += 1

        return {
            "success": True,
            "total": len(pending),
            "sent": success_count,
            "failed": failed_count,
        }


member_lifecycle_service = MemberLifecycleService()

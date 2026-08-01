"""Shared Kanban notification subscription helpers."""

from __future__ import annotations

import logging
import os
from typing import Any

from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)


def maybe_auto_subscribe_create(conn: Any, task_id: str) -> bool:
    """Auto-subscribe the current persistent messaging origin to a task.

    Returns True when a notify-subscription row is written, False when the
    config gate is off, no routable origin exists, the origin is a non-messaging
    surface, or best-effort subscription bookkeeping fails.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # Keep the historical fail-open default: a config read problem should
        # not silently disable notification delivery for interactive users.
        pass

    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import NON_MESSAGING_SESSION_SURFACES, get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        platform_id = str(platform or "").strip().lower()

        if platform_id and platform_id in NON_MESSAGING_SESSION_SURFACES:
            return False

        if not platform or not chat_id:
            # TUI / desktop fallback: these local UIs do not expose a messaging
            # platform+chat pair, but a parent session key gives their poller a
            # stable local return address. Do not fall back to HERMES_SESSION_ID:
            # CLI/ACP sessions set it for telemetry and must not subscribe.
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return False
            platform = "tui"
            chat_id = session_key

        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "") or None
        message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "") or ""
        notifier_profile = (
            get_session_env("HERMES_SESSION_PROFILE", "")
            or os.environ.get("HERMES_PROFILE")
        )
        if not notifier_profile:
            try:
                from hermes_cli.profiles import get_active_profile_name

                notifier_profile = get_active_profile_name() or "default"
            except Exception:
                notifier_profile = "default"

        delivery_metadata: dict[str, Any] = {}
        if thread_id:
            delivery_metadata["thread_id"] = thread_id
        if chat_type:
            delivery_metadata["chat_type"] = chat_type
        if (
            str(platform).lower() == "telegram"
            and thread_id
            and (chat_type or "").lower() in {"dm", "direct", "private"}
        ):
            delivery_metadata["telegram_dm_topic_reply_fallback"] = True
            if str(thread_id) not in {"", "1"}:
                delivery_metadata["direct_messages_topic_id"] = str(thread_id)
            if message_id:
                delivery_metadata["telegram_reply_to_message_id"] = str(message_id)

        from hermes_cli import kanban_db as _kb

        _kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=str(platform),
            chat_id=str(chat_id),
            chat_type=chat_type,
            thread_id=thread_id,
            user_id=user_id,
            notifier_profile=notifier_profile,
            delivery_metadata=delivery_metadata or None,
        )
        return True
    except Exception as exc:
        logger.warning(
            "kanban create auto-subscribe failed: %r (platform=%r chat_id_set=%r)",
            exc,
            platform,
            bool(chat_id),
        )
        return False

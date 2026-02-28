"""
消息处理器 - 精简版（仅抓取功能）
功能：未读检测 -> 点击进入 -> 抓取聊天记录 -> 显示
已禁用：Agent 决策、自动回复、媒体发送
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List

from PySide6.QtCore import QObject, Signal, QTimer

from .session_manager import SessionManager
from ..services.browser_service import BrowserService


class MessageProcessor(QObject):
    """消息编排器 - 仅抓取模式"""

    status_changed = Signal(str)
    log_message = Signal(str)
    message_received = Signal(dict)
    chat_data_received = Signal(dict)

    def __init__(self, browser_service: BrowserService, session_manager: SessionManager):
        super().__init__()
        self.browser = browser_service
        self.sessions = session_manager

        self._running = False
        self._page_ready = False
        self._poll_inflight = False

        self._last_processed_marker = ""

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_cycle)

        self.browser.page_loaded.connect(self._on_page_loaded)
        self.browser.url_changed.connect(self._on_url_changed)

    def start(self, interval_ms: int = 4000):
        if self._running:
            return
        if not self._page_ready:
            self.log_message.emit("⚠️ 页面未就绪，等待加载完成")
            return

        self._running = True
        self._poll_timer.start(interval_ms)
        self.status_changed.emit("running")
        self.log_message.emit("🚀 已启动")

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._poll_timer.stop()
        self._poll_inflight = False
        self.status_changed.emit("stopped")
        self.log_message.emit("🛑 已停止")

    def is_running(self) -> bool:
        return self._running

    def force_check(self):
        if not self._poll_inflight:
            self._poll_cycle()

    def reload_media_config(self):
        """占位：媒体配置已禁用"""
        self.log_message.emit("⚠️ 媒体功能已禁用")

    def reload_keyword_config(self):
        """兼容旧入口：转发到媒体重载。"""
        self.reload_media_config()

    def reload_prompt_docs(self):
        """占位：Prompt 重载已禁用"""
        self.log_message.emit("⚠️ Prompt 功能已禁用")

    def _on_page_loaded(self, success: bool):
        self._page_ready = success
        if success:
            self.status_changed.emit("ready")
            self.log_message.emit("✅ 页面加载完成")
        else:
            self.status_changed.emit("error")
            self.log_message.emit("❌ 页面加载失败")

    def _on_url_changed(self, url: str):
        self.log_message.emit(f"🌐 页面地址变化：{url}")

    def _poll_cycle(self):
        if not self._running or not self._page_ready or self._poll_inflight:
            return
        self._poll_inflight = True
        self._check_unread_and_enter()

    def _check_unread_and_enter(self):
        def on_result(success, result):
            if not success:
                self.log_message.emit("⚠️ 检查未读失败")
                self._reset_cycle()
                return

            payload = self._parse_js_payload(result)
            if payload.get("found") and payload.get("clicked"):
                self.log_message.emit(f"🔔 发现未读 ({payload.get('badgeText', 'dot')})，已点击进入")
                QTimer.singleShot(1000, self._grab_chat_data)
                return

            self._reset_cycle()

        self.browser.find_and_click_first_unread(on_result)

    def _grab_chat_data(self):
        """抓取当前会话聊天记录"""
        if not self._running:
            self._reset_cycle()
            return

        self.browser.grab_chat_data(lambda success, result: self._on_chat_data(success, result))

    def grab_and_display_chat_history(self, auto_reply: bool = True):
        """手动抓取聊天记录（抓取测试按钮使用）"""
        self.browser.grab_chat_data(lambda success, result: self._on_chat_data(success, result))

    def _on_chat_data(self, success: bool, result: Any):
        """处理抓取的聊天数据 - 仅显示，不回复"""
        if not success:
            self.log_message.emit("❌ 抓取聊天记录失败")
            self._reset_cycle()
            return

        data = self._parse_js_payload(result)
        messages = data.get("messages", []) or []
        user_name = (data.get("user_name") or "未知用户").strip() or "未知用户"
        chat_session_key = (data.get("chat_session_key") or "").strip()

        if not messages:
            self.log_message.emit(f"⚠️ 用户 {user_name} 暂无可读消息")
            self._reset_cycle()
            return

        # 显示聊天记录
        self._log_chat_history(user_name, messages)

        # 发出抓取数据信号（供 UI 显示）
        self.chat_data_received.emit({
            "user_name": user_name,
            "messages": messages,
            "chat_session_key": chat_session_key,
        })

        self._reset_cycle()

    def test_grab(self, callback: Callable = None):
        """测试抓取功能"""
        def on_data(success, data):
            if callback:
                callback(success, data)
                return
            if success:
                self.log_message.emit(f"测试抓取成功：{str(data)[:180]}")
            else:
                self.log_message.emit("测试抓取失败")

        self.browser.grab_chat_data(on_data)

    def _reset_cycle(self):
        self._poll_inflight = False

    def _parse_js_payload(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    def _log_chat_history(self, user_name: str, messages: List[Dict[str, Any]]):
        self.log_message.emit(f"📋 聊天记录：{user_name}，共 {len(messages)} 条")
        for msg in messages[-12:]:
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            role = "用户" if msg.get("is_user") else "客服"
            self.log_message.emit(f"  {role}: {text}")

    def _hash_id(self, text: str) -> str:
        return hashlib.md5((text or "").encode("utf-8", errors="ignore")).hexdigest()[:10]

    def _build_session_id(self, user_name: str, chat_session_key: str, chat_session_fingerprint: str = "") -> str:
        key = (chat_session_key or "").strip()
        if key:
            return f"chat_{self._hash_id(key)}"
        user_key = f"user_{self._hash_id(user_name)}"
        fingerprint = (chat_session_fingerprint or "").strip()
        if not fingerprint:
            return user_key
        return f"{user_key}_{self._hash_id(fingerprint)[:6]}"

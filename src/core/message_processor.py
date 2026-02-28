"""
消息处理器 - 精简版（带 LLM 回复）
功能：未读检测 -> 点击进入 -> 抓取聊天记录 -> LLM 回复
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Signal, QTimer

from .session_manager import SessionManager
from ..services.browser_service import BrowserService
from ..services.llm_service import LLMService
from ..data.config_manager import ConfigManager


class MessageProcessor(QObject):
    """消息编排器 - 带 LLM 回复"""

    status_changed = Signal(str)
    log_message = Signal(str)
    message_received = Signal(dict)
    chat_data_received = Signal(dict)
    reply_sent = Signal(str, str)

    def __init__(
        self,
        browser_service: BrowserService,
        session_manager: SessionManager,
        llm_service: LLMService,
        config_manager: ConfigManager,
    ):
        super().__init__()
        self.browser = browser_service
        self.sessions = session_manager
        self.llm_service = llm_service
        self.config_manager = config_manager

        self._running = False
        self._page_ready = False
        self._poll_inflight = False
        self._processing_reply = False

        self._last_processed_marker = ""
        self._pending_reply: Optional[Dict[str, Any]] = None

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_cycle)

        self.browser.page_loaded.connect(self._on_page_loaded)
        self.browser.url_changed.connect(self._on_url_changed)

        # 连接 LLM 响应信号
        self.llm_service.reply_ready.connect(self._on_llm_reply)
        self.llm_service.error_occurred.connect(self._on_llm_error)

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
        self._processing_reply = False
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
        if not self._running or not self._page_ready or self._poll_inflight or self._processing_reply:
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
        self.browser.grab_chat_data(lambda success, result: self._on_chat_data(success, result, auto_reply))

    def _on_chat_data(self, success: bool, result: Any, auto_reply: bool = True):
        """处理抓取的聊天数据"""
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

        if not auto_reply:
            self._reset_cycle()
            return

        # 获取最后一条用户消息
        latest_user_message = self._latest_user_text(messages)
        if not latest_user_message:
            self.log_message.emit("⏸️ 最后一条不是用户消息，跳过自动回复")
            self._reset_cycle()
            return

        # 检查重复消息
        marker = self._build_message_marker(user_name, latest_user_message, messages)
        if marker == self._last_processed_marker:
            self.log_message.emit("⏸️ 检测到重复消息，跳过")
            self._reset_cycle()
            return

        self._last_processed_marker = marker
        self.message_received.emit({"user_name": user_name, "text": latest_user_message})

        # 调用 LLM 回复
        self._processing_reply = True
        self._pending_reply = {
            "user_name": user_name,
            "latest_user_text": latest_user_message,
        }

        # 构建客服 prompt
        system_prompt = f"""你是假发店温柔客服，专服务中老年人，说话简短口语、像真人。
规则：
1. 只回答假发相关问题
2. 每句不超 15 字
3. 语气亲切自然，无 AI 腔
4. 只给结论 + 简单好处
5. 每句话结尾加 emoji
6. 禁止出现任何联系方式
用户问题：{latest_user_message}"""

        self.log_message.emit(f"🤖 正在调用 LLM 回复：{latest_user_message[:30]}...")
        self.llm_service.generate_reply(latest_user_message, system_prompt=system_prompt)

    def _on_llm_reply(self, request_id: str, reply_text: str):
        """LLM 回复响应"""
        if not self._pending_reply:
            self._reset_cycle()
            return

        user_name = self._pending_reply.get("user_name", "未知用户")

        self.log_message.emit(f"✅ LLM 回复生成成功：{reply_text[:50]}...")

        # 发送回复
        self._send_reply(reply_text)

    def _on_llm_error(self, request_id: str, error: str):
        """LLM 错误处理"""
        self.log_message.emit(f"❌ LLM 回复失败：{error}")
        self._pending_reply = None
        self._reset_cycle()

    def _send_reply(self, reply_text: str):
        """发送回复到微信"""
        if not self._pending_reply:
            self._reset_cycle()
            return

        user_name = self._pending_reply.get("user_name", "未知用户")

        def on_text_sent(success, result):
            if not success:
                self.log_message.emit("❌ 文本发送失败")
                self._reset_cycle()
                return

            self.log_message.emit(f"✅ 回复已发送：{reply_text}")
            self.reply_sent.emit(user_name, reply_text)
            self._pending_reply = None
            self._reset_cycle()

        self.browser.send_message(reply_text, on_text_sent)

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
        self._processing_reply = False

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

    def _latest_user_text(self, messages: List[Dict[str, Any]]) -> str:
        if not messages:
            return ""
        if not messages[-1].get("is_user", False):
            return ""
        return (messages[-1].get("text") or "").strip()

    def _build_message_marker(self, user_name: str, latest_user_text: str, messages: List[Dict[str, Any]]) -> str:
        user_count = len([m for m in messages if m.get("is_user")])
        raw = f"{user_name}|{latest_user_text}|{user_count}"
        return self._hash_id(raw)

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

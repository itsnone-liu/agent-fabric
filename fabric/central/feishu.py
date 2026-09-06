"""飞书长连接 channel（lark-oapi WS 模式，无需公网回调/白名单IP）。

⚠️ 互斥警告：同一飞书应用的长连接事件会被所有在线客户端消费。
本机现有 dsh feishu 桥（dsh --profile feishu）若用的是同一个 App，
必须先停旧桥或为 agent-fabric 单独建一个新应用，否则会出现双回复/事件竞争。

启用条件（全部满足才启动）：
  AF_FEISHU_ENABLED=1 + AF_FEISHU_APP_ID + AF_FEISHU_APP_SECRET
  （可选）AF_FEISHU_ALLOWED_OPENIDS=ou_xxx,ou_yyy — 白名单，空=放行所有人（不建议）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                     P2ImMessageReceiveV1, ReplyMessageRequest,
                                     ReplyMessageRequestBody)
    HAS_LARK = True
except Exception:  # lark-oapi 未安装
    HAS_LARK = False


class FeishuChannel:
    name = "feishu"

    def __init__(self, on_text):
        """on_text: async (text: str, open_id: str) -> str"""
        self.on_text = on_text
        self.app_id = os.getenv("AF_FEISHU_APP_ID", "")
        self.app_secret = os.getenv("AF_FEISHU_APP_SECRET", "")
        self.allowed = {x.strip() for x in os.getenv("AF_FEISHU_ALLOWED_OPENIDS", "").split(",") if x.strip()}
        self.enabled = bool(HAS_LARK and self.app_id and self.app_secret)
        if not HAS_LARK:
            print("[feishu] lark-oapi 未安装：pip install 'agent-fabric[feishu]'", flush=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.client = None

    # ---- 生命周期 ----
    def start(self):
        if not self.enabled:
            return
        self.client = (lark.Client.builder()
                       .app_id(self.app_id).app_secret(self.app_secret)
                       .log_level(lark.LogLevel.WARNING).build())
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="feishu-loop").start()
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._on_message).build())
        ws = lark.ws.Client(self.app_id, self.app_secret,
                            event_handler=handler, log_level=lark.LogLevel.INFO)
        threading.Thread(target=ws.start, daemon=True, name="feishu-ws").start()

    # ---- 收消息（lark ws 线程回调）----
    def _on_message(self, data: P2ImMessageReceiveV1):
        try:
            ev = data.event
            msg = ev.message
            if (msg.chat_type or "") != "p2p":  # V0 只处理单聊
                return
            open_id = ev.sender.sender_id.open_id
            if self.allowed and open_id not in self.allowed:
                # open_id 按 App 隔离：把真实 id 回显给用户，管理员据此填白名单
                print(f"[feishu] 未授权访问 from={open_id}", flush=True)
                self._reply(msg.message_id, f"未授权用户。你的 open_id（本应用命名空间）：{open_id}\n请把它提供给管理员加入白名单。")
                return
            content = json.loads(msg.content or "{}")
            text = re.sub(r"@_user_\d+", "", content.get("text", "")).strip()
            if not text:
                return
            print(f"[feishu] 收到消息 from={open_id} text={text[:60]!r}", flush=True)
            fut = asyncio.run_coroutine_threadsafe(self.on_text(text, open_id), self._loop)
            reply = fut.result(timeout=180)
            if reply:
                self._reply(msg.message_id, str(reply))
        except Exception as e:
            print(f"[feishu] 消息处理异常: {e!r}", flush=True)

    def _reply(self, message_id: str, text: str):
        try:
            req = (ReplyMessageRequest.builder().message_id(message_id)
                   .request_body(ReplyMessageRequestBody.builder()
                                 .content(json.dumps({"text": text[:4000]}))
                                 .msg_type("text").build()).build())
            resp = self.client.im.v1.message.reply(req)
            if not resp.success():
                print(f"[feishu] 回复失败 code={resp.code} msg={resp.msg}", flush=True)
        except Exception as e:
            print(f"[feishu] 回复异常: {e!r}", flush=True)

    # ---- 主动推送（任务完成 broadcast → 白名单用户）----
    async def send(self, text: str, **kw):
        if not self.client:
            return
        for open_id in self.allowed:
            try:
                req = (CreateMessageRequest.builder().receive_id_type("open_id")
                       .request_body(CreateMessageRequestBody.builder()
                                     .receive_id(open_id)
                                     .content(json.dumps({"text": text[:4000]}))
                                     .msg_type("text").build()).build())
                resp = self.client.im.v1.message.create(req)
                if not resp.success():
                    print(f"[feishu] 推送失败 code={resp.code}", flush=True)
            except Exception as e:
                print(f"[feishu] 推送异常: {e!r}", flush=True)

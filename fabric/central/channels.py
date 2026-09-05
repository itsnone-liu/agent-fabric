"""Channel 抽象（PLAN §3/§33：入口不属于 Agent 本身；V0 只有 Console，飞书见 feishu.py）。"""
from __future__ import annotations


class Channel:
    name = "base"

    async def send(self, text: str, **kw):
        raise NotImplementedError


class ConsoleChannel(Channel):
    name = "console"

    async def send(self, text: str, **kw):
        print(f"[channel→user]\n{text}\n", flush=True)


class ChannelHub:
    def __init__(self, channels: list[Channel] | None = None):
        self.channels: list[Channel] = list(channels or [])

    def add(self, ch: Channel):
        self.channels.append(ch)

    async def broadcast(self, text: str, **kw):
        for c in self.channels:
            try:
                await c.send(text, **kw)
            except Exception as e:
                print(f"[hub] channel {c.name} 发送失败: {e!r}", flush=True)

import asyncio
import random
import yaml
import os
import json
from typing import Dict, List
from datetime import datetime, timedelta
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.platform import PlatformAdapterType, AiocqhttpAdapter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import astrbot.api.message_components as Comp


@register("revolver_game","zgojin, piexian","群聊左轮手枪游戏插件，支持随机走火事件","1.8.0","https://github.com/piexian/astrbot_plugin_rg")
class RevolverGamePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        plugin_config = config.get("plugin_config", {})
        self.misfire_probability = float(plugin_config.get("misfire_probability", 0.005))
        self.default_misfire_switch = bool(plugin_config.get("default_misfire_switch", False))
        ban_cfg = plugin_config.get("ban_time_range", {})
        self.ban_time_min = int(ban_cfg.get("min", 60))
        self.ban_time_max = int(ban_cfg.get("max", 3000))
        self.game_timeout = int(plugin_config.get("game_timeout", 180))

        # 游戏状态
        self.group_states: Dict[str, Dict[str, List[bool]]] = {}
        self.group_misfire_switches: Dict[str, bool] = {}
        self.texts: Dict = {}

        # 数据目录
        self.plugin_data_dir = StarTools.get_data_dir() / "astrbot_plugin_rg"
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.texts_file = self.plugin_data_dir / "revolver_game_texts.yml"

        self._load_texts()
        self._load_misfire_switches()

        self.scheduler = AsyncIOScheduler()
        if not self.scheduler.running:
            self.scheduler.start()
        self._auto_save_task = asyncio.create_task(self._auto_save_config())

    # ---------- 工具函数 ----------
    def _load_texts(self):
        default_texts = {
            "misfire_switches": {},
            "misfire_descriptions": [
                "突然，左轮手枪走火了！",
                "意外发生了！手枪突然走火！",
                "砰！手枪竟然走火了！"
            ],
            "user_reactions": [
                "{sender}被击中了，痛苦地倒下了",
                "{sender}中弹了，表情非常痛苦",
                "可怜的{sender}被子弹击中了"
            ],
            "trigger_descriptions": ["砰！枪响了", "突然一声枪响", "命中目标"],
            "miss_messages": [
                "是空枪！{sender}安全了",
                "咔嚓，{sender}运气不错",
                "{sender}逃过一劫"
            ],
            "timeout_messages": [
                "【游戏超时】{group_name} 的左轮游戏已结束！如需继续游戏，请发送 /装填 [1-6]"
            ]
        }
        if not self.texts_file.exists():
            with open(self.texts_file, "w", encoding="utf-8") as f:
                yaml.dump(default_texts, f, allow_unicode=True)
            self.texts = default_texts
        else:
            with open(self.texts_file, "r", encoding="utf-8") as f:
                self.texts = yaml.safe_load(f) or default_texts

    def _load_misfire_switches(self):
        self.group_misfire_switches = self.texts.get("misfire_switches", {})

    def _save_misfire_switches(self):
        self.texts["misfire_switches"] = self.group_misfire_switches
        with open(self.texts_file, "w", encoding="utf-8") as f:
            yaml.dump(self.texts, f, allow_unicode=True)

    def _init_group_switch(self, group_id: str):
        if group_id not in self.group_misfire_switches:
            self.group_misfire_switches[group_id] = self.default_misfire_switch

    def _start_timer(self, group_id: str):
        self.scheduler.add_job(
            self._timeout,
            "date",
            run_date=datetime.now() + timedelta(seconds=self.game_timeout),
            args=[group_id],
            id=f"revolver_timer_{group_id}",
            replace_existing=True
        )

    async def _timeout(self, group_id: str):
        if group_id not in self.group_states:
            return
        del self.group_states[group_id]
        msg = random.choice(self.texts.get("timeout_messages", [""])).format(group_name=group_id)
        try:
            platform = self.context.get_platform(PlatformAdapterType.AIOCQHTTP)
            if isinstance(platform, AiocqhttpAdapter):
                await platform.get_client().api.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=msg
                )
        except Exception as e:
            logger.error(f"超时消息发送失败: {e}")

    async def _auto_save_config(self):
        while True:
            await asyncio.sleep(300)
            self._save_misfire_switches()

    # ---------- 指令 ----------
    @filter.command("装填", alias={"load"})
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP | PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def cmd_load(self, event: AstrMessageEvent, num: int = 1):
        group_id = str(event.message_obj.group_id)
        sender = event.get_sender_name() or "用户"
        num = max(1, min(6, num))

        if group_id in self.group_states and any(self.group_states[group_id].get("chambers", [])):
            yield event.plain_result("当前游戏未结束，不能重新装填哦~")
            return

        chambers = [False] * 6
        for pos in random.sample(range(6), num):
            chambers[pos] = True
        self.group_states[group_id] = {"chambers": chambers, "index": 0}
        self._start_timer(group_id)
        yield event.plain_result(f"{sender}装填了{num}发子弹，游戏开始！")

    @filter.command("走火开")
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP | PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def cmd_misfire_on(self, event: AstrMessageEvent):
        gid = str(event.message_obj.group_id)
        self._init_group_switch(gid)
        self.group_misfire_switches[gid] = True
        self._save_misfire_switches()
        yield event.plain_result("走火功能已开启！")

    @filter.command("走火关")
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP | PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def cmd_misfire_off(self, event: AstrMessageEvent):
        gid = str(event.message_obj.group_id)
        self._init_group_switch(gid)
        self.group_misfire_switches[gid] = False
        self._save_misfire_switches()
        yield event.plain_result("走火功能已关闭！")

    @filter.command("开枪", alias={"shoot"})
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP | PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def cmd_shoot(self, event: AstrMessageEvent):
        gid = str(event.message_obj.group_id)
        sender = event.get_sender_name() or "用户"

        if gid not in self.group_states or not self.group_states[gid].get("chambers"):
            yield event.plain_result("请先装填子弹（指令：/装填 [1-6]）")
            return

        state = self.group_states[gid]
        idx = state["index"]
        is_real = state["chambers"][idx]
        state["index"] = (idx + 1) % 6

        if is_real:
            state["chambers"][idx] = False
            desc = random.choice(self.texts.get("trigger_descriptions", [""]))
            react = random.choice(self.texts.get("user_reactions", [""])).format(sender=sender)
            yield event.plain_result(f"{desc}，{react}！")
            try:
                duration = random.randint(self.ban_time_min, self.ban_time_max)
                platform = self.context.get_platform(PlatformAdapterType.AIOCQHTTP)
                if isinstance(platform, AiocqhttpAdapter):
                    await platform.get_client().api.call_action(
                        "set_group_ban",
                        group_id=int(gid),
                        user_id=int(event.get_sender_id()),
                        duration=duration
                    )
            except Exception as e:
                logger.error(f"禁言失败: {e}")
                yield event.plain_result("禁言失败（可能缺少管理员权限）")
        else:
            msg = random.choice(self.texts.get("miss_messages", [""])).format(sender=sender)
            yield event.plain_result(msg)

    # ---------- 随机走火 ----------
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        gid = str(event.message_obj.group_id)
        sender = event.get_sender_name() or "用户"
        self._init_group_switch(gid)

        if self.group_misfire_switches[gid] and random.random() <= self.misfire_probability:
            desc = random.choice(self.texts.get("misfire_descriptions", [""]))
            react = random.choice(self.texts.get("user_reactions", [""])).format(sender=sender)
            yield event.plain_result(f"{desc} {react}！")
            try:
                duration = random.randint(self.ban_time_min, self.ban_time_max)
                platform = self.context.get_platform(PlatformAdapterType.AIOCQHTTP)
                if isinstance(platform, AiocqhttpAdapter):
                    await platform.get_client().api.call_action(
                        "set_group_ban",
                        group_id=int(gid),
                        user_id=int(event.get_sender_id()),
                        duration=duration
                    )
            except Exception as e:
                logger.error(f"走火禁言失败: {e}")

    async def terminate(self):
        """插件重载或停用时执行"""
        self.scheduler.shutdown(wait=False)
        self._save_misfire_switches()
        if hasattr(self, "_auto_save_task"):
            self._auto_save_task.cancel()

import asyncio
import os
import random
import datetime
import yaml
from astrbot.api.all import *
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

@register(
    plugin_name="astrbot_plugin_rg",
    author="原作者zgojin",
    description="群聊左轮手枪游戏插件，支持随机走火事件",
    version="1.9.0",
    repo="https://github.com/piexian/astrbot_plugin_rg"
)
class RevolverGamePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.plugin_config = config or {}
        
        # 指令配置
        self.command_prefix = self.plugin_config.get("command_prefix", "/").strip()
        self.command_load = self.plugin_config.get("command_load", "装填").strip()
        self.command_shoot = self.plugin_config.get("command_shoot", "开枪").strip()
        self.command_misfire_on = self.plugin_config.get("command_misfire_on", "走火开").strip()
        self.command_misfire_off = self.plugin_config.get("command_misfire_off", "走火关").strip()
        
        # 功能参数
        self.misfire_probability = float(self.plugin_config.get("misfire_probability", 0.005))
        self.default_misfire_switch = bool(self.plugin_config.get("default_misfire_switch", False))
        self.ban_time_min = int(self.plugin_config.get("ban_time_range", {}).get("min", 60))
        self.ban_time_max = int(self.plugin_config.get("ban_time_range", {}).get("max", 3000))
        self.game_timeout = int(self.plugin_config.get("game_timeout", 180))
        
        # 游戏状态
        self.group_states = {}  # {群ID: {弹仓状态, 当前索引}}
        self.group_misfire_switches = {}  # {群ID: 走火开关状态}
        self.texts = {}  # 提示文本
        
        # 数据目录
        self.plugin_data_dir = StarTools.get_data_dir() / "revolver_game"
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.texts_file = self.plugin_data_dir / "texts.yml"
        
        # 加载配置
        self._load_texts()
        self._load_misfire_switches()
        
        # 调度器
        self.scheduler = getattr(context, "scheduler", None) or AsyncIOScheduler()
        if not self.scheduler.running:
            self.scheduler.start()

    def _load_texts(self):
        """加载提示文本"""
        if self.texts_file.exists():
            try:
                with open(self.texts_file, "r", encoding="utf-8") as f:
                    self.texts = yaml.safe_load(f) or {}
            except Exception as e:
                logger.error(f"加载文本失败: {e}")
                self.texts = {}
        else:
            self.texts = {
                "misfire_descriptions": ["突然，左轮手枪走火了！"],
                "user_reactions": ["{sender}被流弹击中"],
                "trigger_descriptions": ["枪响了"],
                "miss_messages": ["是空枪！{sender}安全了"]
            }
            with open(self.texts_file, "w", encoding="utf-8") as f:
                yaml.dump(self.texts, f, allow_unicode=True)

    def _load_misfire_switches(self):
        """加载走火开关状态"""
        self.group_misfire_switches = self.texts.get("misfire_switches", {})

    def _save_misfire_switches(self):
        """保存走火开关"""
        self.texts["misfire_switches"] = self.group_misfire_switches
        with open(self.texts_file, "w", encoding="utf-8") as f:
            yaml.dump(self.texts, f, allow_unicode=True)

    def _get_full_cmd(self, cmd):
        """生成带前缀的指令"""
        return f"{self.command_prefix}{cmd}" if self.command_prefix else cmd

    async def on_group_message(self, event: AstrMessageEvent):
        """处理群聊消息"""
        # 检查消息类型是否为群聊
        if event.message_type != "group":
            return
        
        msg = event.message_str.strip()
        group_id = event.message_obj.group_id
        sender = event.get_sender_name() or "用户"
        
        # 初始化群走火开关
        if group_id not in self.group_misfire_switches:
            self.group_misfire_switches[group_id] = self.default_misfire_switch
        
        # 处理走火开/关
        if msg == self._get_full_cmd(self.command_misfire_on):
            self.group_misfire_switches[group_id] = True
            self._save_misfire_switches()
            yield event.plain_result("走火功能已开启！")
            return
        if msg == self._get_full_cmd(self.command_misfire_off):
            self.group_misfire_switches[group_id] = False
            self._save_misfire_switches()
            yield event.plain_result("走火功能已关闭！")
            return
        
        # 处理装填
        if msg.startswith(self._get_full_cmd(self.command_load)):
            parts = msg.replace(self.command_prefix, "", 1).split()
            num = int(parts[1]) if len(parts)>=2 and parts[1].isdigit() else 1
            if not (1<=num<=6):
                yield event.plain_result(f"请装填1-6发子弹（例：{self._get_full_cmd(self.command_load)} 3）")
                return
            if group_id in self.group_states and any(self.group_states[group_id]["chambers"]):
                yield event.plain_result("当前游戏未结束，不能重新装填哦~")
                return
            # 初始化弹仓
            chambers = [False]*6
            for pos in random.sample(range(6), num):
                chambers[pos] = True
            self.group_states[group_id] = {"chambers": chambers, "index": 0}
            yield event.plain_result(f"{sender}装填了{num}发子弹，游戏开始！")
            self._start_timer(group_id)
            return
        
        # 处理开枪
        if msg == self._get_full_cmd(self.command_shoot):
            if group_id not in self.group_states or not self.group_states[group_id]["chambers"]:
                yield event.plain_result(f"请先装填子弹（指令：{self._get_full_cmd(self.command_load)}）")
                return
            state = self.group_states[group_id]
            current_idx = state["index"]
            is_real = state["chambers"][current_idx]
            # 更新索引
            state["index"] = (current_idx + 1) % 6
            if is_real:
                # 实弹（禁言）
                state["chambers"][current_idx] = False
                desc = random.choice(self.texts["trigger_descriptions"])
                react = random.choice(self.texts["user_reactions"]).format(sender=sender)
                yield event.plain_result(f"{desc}，{react}！")
                # 禁言操作
                try:
                    duration = random.randint(self.ban_time_min, self.ban_time_max)
                    await event.bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(event.get_sender_id()),
                        duration=duration
                    )
                except Exception as e:
                    logger.error(f"禁言失败: {e}")
            else:
                # 空弹
                miss = random.choice(self.texts["miss_messages"]).format(sender=sender)
                yield event.plain_result(miss)
            # 检查是否结束
            if not any(state["chambers"]):
                del self.group_states[group_id]
                yield event.plain_result("所有子弹已射出，游戏结束！")
            return
        
        # 随机走火
        if self.group_misfire_switches[group_id] and random.random() <= self.misfire_probability:
            desc = random.choice(self.texts["misfire_descriptions"])
            react = random.choice(self.texts["user_reactions"]).format(sender=sender)
            yield event.plain_result(f"{desc} {react}！")
            # 禁言
            try:
                duration = random.randint(self.ban_time_min, self.ban_time_max)
                await event.bot.set_group_ban(
                    group_id=int(group_id),
                    user_id=int(event.get_sender_id()),
                    duration=duration
                )
            except Exception as e:
                logger.error(f"走火禁言失败: {e}")

    async def on_private_message(self, event: AstrMessageEvent):
        """处理私聊消息，提示仅支持群聊"""
        # 检查消息类型是否为私聊
        if event.message_type == "private":
            yield event.plain_result("该游戏仅支持群聊使用哦~")

    def _start_timer(self, group_id):
        """启动超时定时器"""
        self.scheduler.add_job(
            self._timeout, "date",
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=self.game_timeout),
            args=[group_id], id=f"timer_{group_id}", replace_existing=True
        )

    async def _timeout(self, group_id):
        """超时清理"""
        if group_id in self.group_states:
            del self.group_states[group_id]

    async def terminate(self):
        """卸载插件"""
        if self.scheduler.running:
            self.scheduler.shutdown()

import asyncio
from astrbot.api.all import *
from astrbot.api.event import MessageChain
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.core.star import StarTools
import datetime
import yaml
import random
import os
from astrbot.api import logger


# 插件目录
PLUGIN_DIR = os.path.join('data', 'plugins', 'astrbot_plugin_rg')
# 确保插件目录存在
if not os.path.exists(PLUGIN_DIR):
    os.makedirs(PLUGIN_DIR)


# 插件注册
@register(
    "revolver_game",
    "原作者zgojin",
    "群聊左轮手枪游戏插件，支持随机走火事件",
    "1.7.0",
    "https://github.com/piexian/astrbot_plugin_rg"
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
        self.group_states = {}  # {群ID: {chambers: list, index: int}}
        self.group_misfire_switches = {}  # {群ID: bool}
        self.texts = {}  # 提示文本
        
        # 数据目录（规范路径）
        self.plugin_data_dir = StarTools.get_data_dir() / "revolver_game"
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.texts_file = self.plugin_data_dir / "texts.yml"
        
        # 加载配置
        self._load_texts()
        self._load_misfire_switches()
        
        # 调度器（兼容框架内置）
        self.scheduler = getattr(context, "scheduler", None) or AsyncIOScheduler()
        if not self.scheduler.running:
            self.scheduler.start()

    def _load_texts(self):
        """加载提示文本（容错处理）"""
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

    # 核心消息处理方法（框架会自动调用）
    async def handle_message(self, event: AstrMessageEvent):
        """统一处理所有消息，内部区分群聊/私聊"""
        # 判断消息类型（通过group_id是否存在）
        group_id = getattr(event.message_obj, "group_id", None)
        is_group = group_id is not None  # 有group_id则为群聊
        msg = event.message_str.strip()
        sender = event.get_sender_name() or "用户"

        # 私聊消息处理
        if not is_group:
            # 仅在触发插件指令时提示
            valid_cmds = [self._get_full_cmd(c) for c in 
                          [self.command_load, self.command_shoot, self.command_misfire_on, self.command_misfire_off]]
            if any(msg.startswith(cmd) for cmd in valid_cmds):
                yield event.plain_result("该游戏仅支持群聊使用哦~")
            return

        # 群聊消息处理
        self._init_group_switch(group_id)  # 初始化群配置
        
        # 处理走火开
        if msg == self._get_full_cmd(self.command_misfire_on):
            self.group_misfire_switches[group_id] = True
            self._save_misfire_switches()
            yield event.plain_result("走火功能已开启！")
            return
        
        # 处理走火关
        if msg == self._get_full_cmd(self.command_misfire_off):
            self.group_misfire_switches[group_id] = False
            self._save_misfire_switches()
            yield event.plain_result("走火功能已关闭！")
            return
        
        # 处理装填
        if msg.startswith(self._get_full_cmd(self.command_load)):
            async for res in self._handle_load(group_id, sender, msg):
                yield res
            return
        
        # 处理开枪
        if msg == self._get_full_cmd(self.command_shoot):
            async for res in self._handle_shoot(group_id, sender, event):
                yield res
            return
        
        # 随机走火
        if self.group_misfire_switches[group_id] and random.random() <= self.misfire_probability:
            async for res in self._handle_misfire(group_id, sender, event):
                yield res

    def _init_group_switch(self, group_id):
        """初始化群走火开关"""
        if group_id not in self.group_misfire_switches:
            self.group_misfire_switches[group_id] = self.default_misfire_switch

    async def _handle_load(self, group_id, sender, msg):
        """处理装填指令"""
        # 解析子弹数量
        try:
            cmd_part = msg.replace(self.command_prefix, "", 1) if self.command_prefix else msg
            parts = cmd_part.split()
            num = int(parts[1]) if len(parts) >= 2 else 1
            num = max(1, min(6, num))  # 限制1-6
        except (ValueError, IndexError):
            yield event.plain_result(f"请输入正确格式：{self._get_full_cmd(self.command_load)} [1-6]")
            return
        
        # 检查游戏状态
        if group_id in self.group_states and any(self.group_states[group_id].get("chambers", [])):
            yield event.plain_result("当前游戏未结束，不能重新装填哦~")
            return
        
        # 初始化弹仓
        chambers = [False] * 6
        for pos in random.sample(range(6), num):
            chambers[pos] = True
        self.group_states[group_id] = {"chambers": chambers, "index": 0}
        yield event.plain_result(f"{sender}装填了{num}发子弹，游戏开始！")
        self._start_timer(group_id)

    async def _handle_shoot(self, group_id, sender, event):
        """处理开枪指令"""
        if group_id not in self.group_states or not self.group_states[group_id].get("chambers"):
            yield event.plain_result(f"请先装填子弹（指令：{self._get_full_cmd(self.command_load)}）")
            return
        
        state = self.group_states[group_id]
        current_idx = state["index"]
        is_real = state["chambers"][current_idx]
        
        # 更新索引
        state["index"] = (current_idx + 1) % 6
        
        if is_real:
            # 实弹：禁言+提示
            state["chambers"][current_idx] = False
            desc = random.choice(self.texts.get("trigger_descriptions", ["枪响了"]))
            react = random.choice(self.texts.get("user_reactions", ["{sender}被击中"])).format(sender=sender)
            yield event.plain_result(f"{desc}，{react}！")
            
            # 执行禁言
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
            # 空弹：提示
            miss = random.choice(self.texts.get("miss_messages", ["是空枪！{sender}安全了"])).format(sender=sender)
            yield event.plain_result(miss)
        
        # 检查游戏结束
        if not any(state["chambers"]):
            del self.group_states[group_id]
            yield event.plain_result("所有子弹已射出，游戏结束！")

    async def _handle_misfire(self, group_id, sender, event):
        """处理随机走火"""
        desc = random.choice(self.texts.get("misfire_descriptions", ["突然，左轮手枪走火了！"]))
        react = random.choice(self.texts.get("user_reactions", ["{sender}被流弹击中"])).format(sender=sender)
        yield event.plain_result(f"{desc} {react}！")
        
        # 走火禁言
        try:
            duration = random.randint(self.ban_time_min, self.ban_time_max)
            await event.bot.set_group_ban(
                group_id=int(group_id),
                user_id=int(event.get_sender_id()),
                duration=duration
            )
        except Exception as e:
                logger.error(f"走火禁言失败: {e}")

    def _start_timer(self, group_id):
        """启动超时定时器"""
        self.scheduler.add_job(
            self._timeout, "date",
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=self.game_timeout),
            args=[group_id], id=f"timer_{group_id}", replace_existing=True
        )

    async def _timeout(self, group_id):
        """超时清理游戏状态"""
        if group_id in self.group_states:
            del self.group_states[group_id]
            logger.info(f"群{group_id}游戏超时，已结束")

    async def terminate(self):
        """插件卸载清理"""
        if hasattr(self, "scheduler") and self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("左轮手枪插件已卸载")

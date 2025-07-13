import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.star import StarTools
import datetime
import yaml
import random
import os
import json
from typing import Optional, Dict, List, Union, Any
import datetime
import astrbot.api.message_components as Comp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.core.config import AstrBotConfig

@register("revolver_game","原作者：zgojin，修正者：piexian","群聊左轮手枪游戏插件，支持随机走火事件","1.7.3","https://github.com/piexian/astrbot_plugin_rg")
class RevolverGamePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.plugin_config = config.get('plugin_config', {})
        
        # 指令配置 - 使用get方法并提供默认值
        self.command_load = self.plugin_config.get("command_load", "装填").strip()
        self.command_shoot = self.plugin_config.get("command_shoot", "开枪").strip()
        self.command_misfire_on = self.plugin_config.get("command_misfire_on", "走火开").strip()
        self.command_misfire_off = self.plugin_config.get("command_misfire_off", "走火关").strip()
        
        # 功能参数
        self.misfire_probability = float(self.plugin_config.get("misfire_probability", 0.005))
        self.default_misfire_switch = bool(self.plugin_config.get("default_misfire_switch", False))
        
        ban_time_range = self.plugin_config.get("ban_time_range", {})
        self.ban_time_min = int(ban_time_range.get("min", 60))
        self.ban_time_max = int(ban_time_range.get("max", 3000))
        self.game_timeout = int(self.plugin_config.get("game_timeout", 180))
        
        # 游戏状态（键：群ID，值：游戏数据）
        self.group_states: Dict[str, Dict] = {}  # {群ID: {chambers: List[bool], index: int}}
        self.group_misfire_switches: Dict[str, bool] = {}  # {群ID: 走火开关状态}
        self.texts: Dict = {}  # 提示文本
        
        # 数据目录（符合文档规范：存储在data目录下）
        self.plugin_data_dir = StarTools.get_data_dir() / "plugin_data" / "astrbot_plugin_rg"
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.texts_file = self.plugin_data_dir / "revolver_game_texts.yml"
        
        # 加载配置
        self._load_texts()
        self._load_misfire_switches()
        
        # 调度器
        self.scheduler = getattr(context, "scheduler", None) or AsyncIOScheduler()
        if not self.scheduler.running:
            self.scheduler.start()
    def _load_texts(self):
        """加载游戏文本，如果不存在则创建默认文本"""
        if not hasattr(self, '_cached_texts'):
            # 默认文本
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
                "trigger_descriptions": [
                    "砰！枪响了",
                    "突然一声枪响",
                    "命中目标"
                ],
                "miss_messages": [
                    "是空枪！{sender}安全了",
                    "咔嚓，{sender}运气不错",
                    "{sender}逃过一劫"
                ]
            }

            if not self.texts_file.exists():
                # 如果文件不存在，创建默认文本文件
                self.texts_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.texts_file, 'w', encoding='utf-8') as f:
                    yaml.dump(default_texts, f, allow_unicode=True)
                self._cached_texts = default_texts
            else:
                # 如果文件存在，尝试读取
                encodings = ['utf-8', 'gbk', 'gb2312']
                for encoding in encodings:
                    try:
                        with open(self.texts_file, 'r', encoding=encoding) as f:
                            self._cached_texts = yaml.safe_load(f) or default_texts
                            break
                    except UnicodeDecodeError:
                        continue
                    except Exception as e:
                        logger.error(f"读取文本文件失败: {e}")
                        self._cached_texts = default_texts
                        break
                else:
                    self._cached_texts = default_texts
            
            self.texts = self._cached_texts
        return self._cached_texts
        
    def _load_misfire_switches(self):
        """加载走火开关状态（持久化）"""
        self.group_misfire_switches = self.texts.get("misfire_switches", {})

    def _save_misfire_switches(self):
        """保存走火开关状态"""
        self.texts["misfire_switches"] = self.group_misfire_switches
        with open(self.texts_file, "w", encoding="utf-8") as f:
            yaml.dump(self.texts, f, allow_unicode=True)

    def _init_group_switch(self, group_id: str):
        """初始化群走火开关（默认关闭）"""
        if group_id not in self.group_misfire_switches:
            self.group_misfire_switches[group_id] = self.default_misfire_switch

    def _start_timer(self, group_id: str):
        """启动游戏超时定时器（使用框架调度器）"""
        self.scheduler.add_job(
            self._timeout, "date",
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=self.game_timeout),
            args=[group_id], id=f"revolver_timer_{group_id}", replace_existing=True
        )

    async def _timeout(self, group_id: str):
        """超时清理游戏状态并发送播报"""
        if group_id in self.group_states:
            # 获取群名称（如果可能）
            group_name = f"群{group_id}"
            bot = getattr(self.context, "bot", None)
            
            # 尝试获取群名称
            if bot and hasattr(bot, "get_group_info"):
                try:
                    group_info = await bot.get_group_info(group_id=int(group_id))
                    group_name = group_info.get("group_name", group_name)
                except Exception as e:
                    logger.warning(f"获取群名称失败: {e}")
                    group_name = group_id
            # 清理游戏状态
            del self.group_states[group_id]
            logger.info(f"{group_name}({group_id}) 游戏超时，已结束")
            
            # 发送超时播报
            timeout_msg = random.choice(self.texts.get("timeout_messages", [
                f"【游戏超时】{group_name} 的左轮游戏已结束！如需继续游戏，请发送 /装填 [1-6]"
            ]))
            
            # 使用框架的消息发送API
            try:
                await self.bot.send_group_msg(group_id=group_id, message=timeout_msg)
            except Exception as e:
                logger.error(f"超时消息发送失败: {e}")
    # ------------------------------
    # 指令处理
    # ------------------------------

    @filter.command("command_misfire_on", priority=1)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)  # 仅群聊生效
    async def handle_misfire_on(self, event: AstrMessageEvent):
        """开启当前群的走火功能"""
        group_id = event.message_obj.group_id
        self._init_group_switch(group_id)
        self.group_misfire_switches[group_id] = True
        self._save_misfire_switches()
        yield event.plain_result("走火功能已开启！")

    @filter.command("command_misfire_off", priority=1)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_misfire_off(self, event: AstrMessageEvent):
        """关闭当前群的走火功能"""
        group_id = event.message_obj.group_id
        self._init_group_switch(group_id)
        self.group_misfire_switches[group_id] = False
        self._save_misfire_switches()
        yield event.plain_result("走火功能已关闭！")

    @filter.command("command_load", priority=1)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_load(self, event: AstrMessageEvent ):
        """装填子弹（默认1发，支持1-6发）\n指令：/装填 [1-6]"""
        group_id = event.message_obj.group_id
        sender = event.get_sender_name() or "用户"
        
        # 校验子弹数量
        try:
            args = event.get_args()
            num = int(args[0]) if args else 1
            num = max(1, min(6, num))  # 限制1-6发
        except (ValueError, TypeError):
            yield event.plain_result("请输入正确格式：/装填 [1-6]（1-6之间的数字）")
            return
        
        # 检查游戏状态（是否已装填）
        if group_id in self.group_states and any(self.group_states[group_id].get("chambers", [])):
            yield event.plain_result("当前游戏未结束，不能重新装填哦~")
            return
        
        # 初始化弹仓（6个位置，随机放入num发子弹）
        chambers = [False] * 6
        for pos in random.sample(range(6), num):
            chambers[pos] = True
        self.group_states[group_id] = {"chambers": chambers, "index": 0}
        
        yield event.plain_result(f"{sender}装填了{num}发子弹，游戏开始！")
        self._start_timer(group_id)

    @filter.command("command_shoot", priority=1)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_shoot(self, event: AstrMessageEvent):
        """触发开枪（轮流进行，命中会被禁言）"""
        group_id = event.message_obj.group_id
        sender = event.get_sender_name() or "用户"
        
        # 检查是否已装填子弹
        if group_id not in self.group_states or not self.group_states[group_id].get("chambers"):
            yield event.plain_result(f"请先装填子弹（指令：/装填 [1-6]）")
            return
        
        # 获取当前游戏状态
        state = self.group_states[group_id]
        current_idx = state["index"]
        is_real = state["chambers"][current_idx]
        
        # 更新下一轮索引（循环6个位置）
        state["index"] = (current_idx + 1) % 6
        
        if is_real:
            # 命中：禁言+提示
            state["chambers"][current_idx] = False  # 移除该子弹
            desc = random.choice(self.texts.get("trigger_descriptions", ["枪响了"]))
            react = random.choice(self.texts.get("user_reactions", ["{sender}被击中"])).format(sender=sender)
            yield event.plain_result(f"{desc}，{react}！")
            
            # 执行禁言（仅支持aiocqhttp平台）
            if isinstance(event, AiocqhttpMessageEvent):
                try:
                    duration = random.randint(self.ban_time_min, self.ban_time_max)
                    await event.bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(event.get_sender_id()),
                        duration=duration
                    )
                except Exception as e:
                    logger.error(f"禁言失败: {e}")
                    yield event.plain_result("禁言失败（可能缺少管理员权限）")
            else:
                yield event.plain_result("当前平台不支持禁言功能")
        else:
            # 空枪：提示安全
            miss = random.choice(self.texts.get("miss_messages", ["是空枪！{sender}安全了"])).format(sender=sender)
            yield event.plain_result(miss)
        
        # 检查游戏是否结束（所有子弹已射出）
        if not any(state["chambers"]):
            del self.group_states[group_id]
            yield event.plain_result("所有子弹已射出，游戏结束！")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL)
    async def handle_random_misfire(self, event: AstrMessageEvent):
        """随机走火检测（群消息触发低概率走火）"""
        group_id = event.message_obj.group_id
        sender = event.get_sender_name() or "用户"
        self._init_group_switch(group_id)
        
        # 检查走火开关是否开启且随机命中概率
        if self.group_misfire_switches[group_id] and random.random() <= self.misfire_probability:
            desc = random.choice(self.texts.get("misfire_descriptions", ["突然，左轮手枪走火了！"]))
            react = random.choice(self.texts.get("user_reactions", ["{sender}被流弹击中"])).format(sender=sender)
            yield event.plain_result(f"{desc} {react}！")
            
            # 走火禁言
            if isinstance(event, AiocqhttpMessageEvent):
                try:
                    duration = random.randint(self.ban_time_min, self.ban_time_max)
                    await event.bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(event.get_sender_id()),
                        duration=duration
                    )
                except Exception as e:
                    logger.error(f"走火禁言失败: {e}")
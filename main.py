import asyncio
import os
import random
import datetime
import yaml
from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 注意：register装饰器建议使用关键字参数，避免版本兼容问题
@register(
    plugin_name="astrbot_plugin_rg",
    author="原作者zgojin",
    description="群聊左轮手枪游戏插件，玩家可以在群聊中进行左轮手枪射击游戏，支持随机走火事件",
    version="1.7.",
    repo="https://github.com/piexian/astrbot_plugin_rg"
)
class RevolverGamePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        logger.info("===== 左轮手枪插件初始化开始 =====")  # 调试日志
        
        # 配置兼容处理（避免config为空）
        self.plugin_config = config or {}
        logger.info(f"加载插件配置: {self.plugin_config}")
        
        # 核心指令配置（强制字符串类型，避免类型错误）
        self.command_prefix = str(self.plugin_config.get("command_prefix", "/")).strip()
        self.command_load = str(self.plugin_config.get("command_load", "装填")).strip()
        self.command_shoot = str(self.plugin_config.get("command_shoot", "开枪")).strip()
        self.command_misfire_on = str(self.plugin_config.get("command_misfire_on", "走火开")).strip()
        self.command_misfire_off = str(self.plugin_config.get("command_misfire_off", "走火关")).strip()
        logger.info(f"指令配置: 前缀={self.command_prefix}, 装填={self.command_load}, 射击={self.command_shoot}")
        
        # 功能参数（类型转换+边界处理）
        self.misfire_probability = max(0.0, min(1.0, float(self.plugin_config.get("misfire_probability", 0.005))))
        self.default_misfire_switch = bool(self.plugin_config.get("default_misfire_switch", False))
        self.ban_time_min = max(0, int(self.plugin_config.get("ban_time_range", {}).get("min", 60)))
        self.ban_time_max = max(self.ban_time_min, int(self.plugin_config.get("ban_time_range", {}).get("max", 3000)))
        self.game_timeout = max(10, int(self.plugin_config.get("game_timeout", 180)))  # 最小10秒
        logger.info(f"功能参数: 走火概率={self.misfire_probability}, 禁言范围={self.ban_time_min}-{self.ban_time_max}s")
        
        # 游戏状态初始化
        self.group_states = {}  # {group_id: {chambers: list, current_chamber_index: int}}
        self.group_misfire_switches = {}  # {group_id: bool}
        self.texts = {}  # 提示文本
        self._load_misfire_switches()  # 加载走火开关
        self._load_texts()  # 加载文本
        
        # 调度器初始化（兼容AstrBot内置调度器）
        self.scheduler = getattr(context, "scheduler", None)
        if not self.scheduler:
            self.scheduler = AsyncIOScheduler()
            context.scheduler = self.scheduler
            try:
                self.scheduler.start()
                logger.info("调度器初始化成功")
            except Exception as e:
                logger.error(f"调度器启动失败: {e}")
        
        # 数据目录（确保路径正确）
        self.plugin_data_dir = StarTools.get_data_dir() / "astrbot_plugin_rg"  # 独立目录
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.texts_file = self.plugin_data_dir / "revolver_game_texts.yml"
        logger.info(f"数据目录: {self.plugin_data_dir}, 文本文件: {self.texts_file}")
        
        logger.info("===== 左轮手枪插件初始化完成 =====")

    def _load_texts(self):
        """加载提示文本（容错处理）"""
        try:
            if self.texts_file.exists():
                with open(self.texts_file, "r", encoding="utf-8") as f:
                    self.texts = yaml.safe_load(f) or {}
                    logger.info("提示文本加载成功")
            else:
                # 初始化默认文本（避免文件不存在导致错误）
                self.texts = {
                    "misfire_descriptions": ["突然，左轮手枪走火了！"],
                    "user_reactions": ["{sender_nickname}被流弹击中"],
                    "trigger_descriptions": ["枪响了"],
                    "miss_messages": ["是空枪！{sender_nickname}安全了"]
                }
                with open(self.texts_file, "w", encoding="utf-8") as f:
                    yaml.dump(self.texts, f, allow_unicode=True)
                logger.info("默认提示文本已创建")
        except Exception as e:
            logger.error(f"加载提示文本失败: {e}")
            self.texts = {"misfire_descriptions": [], "user_reactions": [], "trigger_descriptions": [], "miss_messages": []}

    def _load_misfire_switches(self):
        """加载走火开关（从文本文件）"""
        try:
            self.group_misfire_switches = self.texts.get("misfire_switches", {})
            logger.info(f"走火开关加载成功，共{len(self.group_misfire_switches)}个群配置")
        except Exception as e:
            logger.error(f"加载走火开关失败: {e}")
            self.group_misfire_switches = {}

    def _save_misfire_switches(self):
        """保存走火开关到文件"""
        try:
            self.texts["misfire_switches"] = self.group_misfire_switches
            with open(self.texts_file, "w", encoding="utf-8") as f:
                yaml.dump(self.texts, f, allow_unicode=True)
            logger.info("走火开关保存成功")
        except Exception as e:
            logger.error(f"保存走火开关失败: {e}")

    def _get_full_command(self, command: str) -> str:
        """生成带前缀的完整指令"""
        return f"{self.command_prefix}{command}" if self.command_prefix else command

    def _is_command(self, message_str: str, command: str) -> bool:
        """判断消息是否为目标指令"""
        return message_str.strip() == self._get_full_command(command)

    def _strip_prefix(self, message_str: str) -> str:
        """移除指令前缀"""
        if self.command_prefix and message_str.startswith(self.command_prefix):
            return message_str[len(self.command_prefix):].strip()
        return message_str

    @filter()  # 监听所有消息
    async def on_all_messages(self, event: AstrMessageEvent):
        """处理所有消息（入口方法）"""
        try:
            # 获取基础信息（兼容不同版本API）
            group_id = getattr(event.message_obj, "group_id", None)
            user_id = getattr(event, "user_id", getattr(event.message_obj, "user_id", None))
            sender_nickname = getattr(event, "sender_name", "用户")
            message_str = event.message_str.strip()
            logger.debug(f"收到消息: 群={group_id}, 用户={user_id}, 内容={message_str}")

            # 私聊处理
            if not group_id:
                valid_commands = [
                    self._get_full_command(self.command_misfire_on),
                    self._get_full_command(self.command_misfire_off),
                    self._get_full_command(self.command_load),
                    self._get_full_command(self.command_shoot)
                ]
                if any(message_str.startswith(cmd) for cmd in valid_commands):
                    yield event.plain_result("该游戏仅限群聊使用，请在群内游玩~")
                return

            # 初始化群配置
            if group_id not in self.group_misfire_switches:
                self.group_misfire_switches[group_id] = self.default_misfire_switch
                logger.debug(f"初始化群{group_id}走火开关: {self.group_misfire_switches[group_id]}")

            # 处理走火开指令
            if self._is_command(message_str, self.command_misfire_on):
                self.group_misfire_switches[group_id] = True
                self._save_misfire_switches()
                yield event.plain_result(f"群{group_id}走火功能已开启！使用{self._get_full_command(self.command_misfire_off)}关闭")
                return

            # 处理走火关指令
            if self._is_command(message_str, self.command_misfire_off):
                self.group_misfire_switches[group_id] = False
                self._save_misfire_switches()
                yield event.plain_result(f"群{group_id}走火功能已关闭！使用{self._get_full_command(self.command_misfire_on)}开启")
                return

            # 处理装填指令
            if message_str.startswith(self._get_full_command(self.command_load)):
                cleaned_msg = self._strip_prefix(message_str)
                num_bullets = self._parse_bullet_count(cleaned_msg)
                async for res in self._handle_load(group_id, sender_nickname, num_bullets):
                    yield res
                return

            # 处理开枪指令
            if self._is_command(message_str, self.command_shoot):
                async for res in self._handle_shoot(group_id, sender_nickname, event):
                    yield res
                return

            # 随机走火
            if self.group_misfire_switches[group_id] and random.random() <= self.misfire_probability:
                misfire_desc = random.choice(self.texts.get("misfire_descriptions", ["突然，左轮手枪走火了！"]))
                user_reaction = random.choice(self.texts.get("user_reactions", ["{sender_nickname}被流弹击中"])).format(sender_nickname=sender_nickname)
                yield event.plain_result(f"{misfire_desc} {user_reaction}！")
                # 禁言处理
                await self._ban_user(event, user_id, group_id)
                return

        except Exception as e:
            logger.error(f"消息处理错误: {e}", exc_info=True)
            yield event.plain_result("插件运行出错，请联系管理员~")

    def _parse_bullet_count(self, message_str: str) -> int:
        """解析装填子弹数量"""
        try:
            parts = message_str.split()
            if len(parts) >= 2 and parts[0] == self.command_load:
                return int(parts[1])
            return 1  # 默认1发
        except Exception as e:
            logger.error(f"解析子弹数量失败: {e}")
            return None

    async def _handle_load(self, group_id: str, sender_nickname: str, num_bullets: int):
        """处理装填指令"""
        try:
            # 验证子弹数量
            if num_bullets is None:
                yield f"请输入有效的子弹数量（1-6），例如: {self._get_full_command(self.command_load)} 3"
                return
            if not (1 <= num_bullets <= 6):
                yield f"子弹数量必须在1-6之间，请重新输入~"
                return

            # 检查游戏状态
            if group_id in self.group_states and any(self.group_states[group_id]["chambers"]):
                yield f"{sender_nickname}，游戏还没结束哦，先打完当前弹匣吧~"
                return

            # 初始化弹匣
            chambers = [False] * 6
            positions = random.sample(range(6), num_bullets)
            for pos in positions:
                chambers[pos] = True
            self.group_states[group_id] = {
                "chambers": chambers,
                "current_chamber_index": 0
            }
            logger.debug(f"群{group_id}装填子弹: {num_bullets}发，弹匣: {chambers}")

            # 启动超时定时器
            self.start_timer(group_id, self.game_timeout)
            yield f"{sender_nickname} 装填了{num_bullets}发实弹到6弹匣左轮，游戏开始！（{self.game_timeout}秒内未操作将结束）"
        except Exception as e:
            logger.error(f"装填处理失败: {e}")
            yield "装填失败，请重试~"

    async def _handle_shoot(self, group_id: str, sender_nickname: str, event: AstrMessageEvent):
        """处理射击指令"""
        try:
            # 检查游戏状态
            if group_id not in self.group_states or not self.group_states[group_id]["chambers"]:
                yield f"{sender_nickname}，请先装填子弹哦（指令: {self._get_full_command(self.command_load)}）"
                return

            # 移除旧定时器，启动新定时器
            self._remove_timer_job(f"timeout_{group_id}")
            self.start_timer(group_id, self.game_timeout)

            # 执行射击
            state = self.group_states[group_id]
            current_index = state["current_chamber_index"]
            is_real_shot = state["chambers"][current_index]

            if is_real_shot:
                # 实弹：禁言+提示
                state["chambers"][current_index] = False
                trigger_desc = random.choice(self.texts.get("trigger_descriptions", ["枪响了"]))
                user_reaction = random.choice(self.texts.get("user_reactions", ["{sender_nickname}被击中了"])).format(sender_nickname=sender_nickname)
                yield f"{trigger_desc}，{user_reaction}！"
                # 禁言操作
                await self._ban_user(event, event.get_sender_id(), group_id)
            else:
                # 空弹：仅提示
                miss_msg = random.choice(self.texts.get("miss_messages", ["是空枪！{sender_nickname}安全了"])).format(sender_nickname=sender_nickname)
                yield miss_msg

            # 更新弹仓索引
            state["current_chamber_index"] = (current_index + 1) % 6

            # 检查游戏是否结束
            if not any(state["chambers"]):
                del self.group_states[group_id]
                yield f"弹匣空了！游戏结束~ 想再玩请重新{self._get_full_command(self.command_load)}"
        except Exception as e:
            logger.error(f"射击处理失败: {e}")
            yield "射击失败，请重试~"

    async def _ban_user(self, event: AstrMessageEvent, user_id: int, group_id: int):
        """禁言用户（兼容API）"""
        try:
            duration = random.randint(self.ban_time_min, self.ban_time_max)
            # 适配不同版本的禁言API
            if hasattr(event.bot, "set_group_ban"):
                await event.bot.set_group_ban(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    duration=duration
                )
                logger.info(f"用户{user_id}在群{group_id}被禁言{duration}秒")
            else:
                logger.warning("禁言API不存在，跳过禁言操作")
        except Exception as e:
            logger.error(f"禁言失败: {e}")

    def start_timer(self, group_id: str, seconds: int):
        """启动超时定时器"""
        job_id = f"timeout_{group_id}"
        try:
            self.scheduler.add_job(
                self._timeout_callback,
                "date",
                run_date=datetime.datetime.now() + datetime.timedelta(seconds=seconds),
                args=[group_id],
                id=job_id,
                replace_existing=True  # 替换已有任务，避免重复
            )
            logger.debug(f"群{group_id}超时定时器启动（{seconds}秒）")
        except Exception as e:
            logger.error(f"定时器启动失败: {e}")

    def _remove_timer_job(self, job_id: str):
        """移除定时器任务"""
        try:
            if self.scheduler and self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.debug(f"定时器任务{job_id}已移除")
        except Exception as e:
            logger.error(f"移除定时器失败: {e}")

    async def _timeout_callback(self, group_id: str):
        """超时回调：结束游戏"""
        if group_id in self.group_states:
            del self.group_states[group_id]
            logger.info(f"群{group_id}游戏超时，已结束")

    async def terminate(self):
        """插件卸载清理"""
        try:
            if self.scheduler:
                self.scheduler.shutdown()
            logger.info("左轮手枪插件已卸载")
        except Exception as e:
            logger.error(f"插件卸载失败: {e}")

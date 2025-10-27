import datetime
import os
import random
import shutil
import yaml
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.all import (
    AstrMessageEvent,
    Context,
    EventMessageType,
    Star,
    command,
    event_message_type,
    register,
)
from astrbot.api import AstrBotConfig, logger

DEFAULT_TEXTS_FILE = os.path.join(os.path.dirname(__file__), 'revolver_game_texts.yml')

CHAMBER_COUNT = 6
DEFAULT_MIN_BAN_DURATION = 60
DEFAULT_MAX_BAN_DURATION = 300
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MISFIRE_PROBABILITY = 0.005

DEFAULT_PLUGIN_CONFIG: Dict[str, Any] = {
    'misfire_probability': DEFAULT_MISFIRE_PROBABILITY,
    'timeout_seconds': DEFAULT_TIMEOUT_SECONDS,
    'min_ban_seconds': DEFAULT_MIN_BAN_DURATION,
    'max_ban_seconds': DEFAULT_MAX_BAN_DURATION,
    'misfire_enabled_by_default': False,
}

DEFAULT_FALLBACK_TEXTS: Dict[str, List[str]] = {
    'misfire_descriptions': ['手枪突然走火。'],
    'user_reactions': ['{sender_nickname} 吓得不轻。'],
    'trigger_descriptions': ['扳机被扣下。'],
    'miss_messages': ['{sender_nickname} 幸运地打出了一发空弹。']
}

@register(
    "astrbot_plugin_rg",
    "zgojin, piexian",
    "群聊左轮手枪游戏插件",
    "1.4.1",
    "https://github.com/piexian/astrbot_plugin_rg",
)
class RevolverGamePlugin(Star):
    COMMAND_KEYWORDS = {"装填", "开枪", "走火开", "走火关"}

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = self._initialize_config(config)
        self.plugin_dir = context.get_plugin_data_dir()
        os.makedirs(self.plugin_dir, exist_ok=True)
        self.texts_file = os.path.join(self.plugin_dir, 'revolver_game_texts.yml')

        # 缓存初始化与文本加载
        self._cached_texts: Optional[Dict[str, Any]] = None
        self._default_texts: Optional[Dict[str, Any]] = None
        self._ensure_texts_file()
        self.texts = self._load_texts()

        # 群游戏状态
        self.group_states: Dict[int, Dict[str, Any]] = {}
        # 加载走火开关
        self.group_misfire_switches = self._load_misfire_switches()
        # 走火概率
        self.misfire_probability = self._load_misfire_probability()
        # 惩罚与超时时间配置
        self.min_ban_seconds, self.max_ban_seconds = self._load_ban_duration_bounds()
        self.timeout_seconds = self._load_timeout_seconds()
        self.default_misfire_enabled = self._load_default_misfire_switch()
        # 初始化定时器调度器
        if not hasattr(context, 'scheduler'):
            context.scheduler = AsyncIOScheduler()
            context.scheduler.start()
        self.scheduler = context.scheduler
        # 群消息来源映射
        self.group_umo_mapping: Dict[int, Any] = {}
        # 注册插件指令
        self._register_commands()

    def _register_commands(self):
        """按照 AstrBot 文档注册插件指令，兼容不同的上下文实现"""
        register_command = getattr(self.context, "register_command", None)
        if not callable(register_command):
            return

        def _safe_register(name: str, handler, description: str, usage: str, aliases: List[str]):
            try:
                register_command(
                    name=name,
                    handler=handler,
                    description=description,
                    usage=usage,
                    aliases=aliases,
                )
            except TypeError:
                try:
                    register_command(
                        name,
                        handler,
                        description=description,
                        usage=usage,
                        aliases=aliases,
                    )
                except TypeError:
                    register_command(name, handler)

        _safe_register(
            name="装填",
            handler=self.command_load,
            description="装填左轮手枪的实弹数量",
            usage="/装填 [1-6]",
            aliases=["装填", "/装填"],
        )
        _safe_register(
            name="开枪",
            handler=self.command_shoot,
            description="扣动扳机进行射击",
            usage="/开枪",
            aliases=["开枪", "/开枪"],
        )
        _safe_register(
            name="走火开",
            handler=self.command_misfire_on,
            description="开启当前群聊的随机走火功能",
            usage="/走火开",
            aliases=["走火开", "/走火开"],
        )
        _safe_register(
            name="走火关",
            handler=self.command_misfire_off,
            description="关闭当前群聊的随机走火功能",
            usage="/走火关",
            aliases=["走火关", "/走火关"],
        )

    def _initialize_config(self, config: Optional[AstrBotConfig]) -> Dict[str, Any]:
        """解析并缓存插件配置"""
        resolved_config: Optional[Dict[str, Any]] = None

        if config is not None:
            resolved_config = config
        else:
            context_getter = getattr(self.context, "get_config", None)
            if callable(context_getter):
                resolved_config = context_getter() or {}

        if resolved_config is None:
            resolved_config = {}

        updated = False
        for key, default_value in DEFAULT_PLUGIN_CONFIG.items():
            if key not in resolved_config:
                resolved_config[key] = default_value
                updated = True

        if updated and hasattr(resolved_config, "save_config"):
            try:
                resolved_config.save_config()
            except Exception as exc:  # pragma: no cover - 防御性日志
                logger.error(f"Failed to persist default config: {exc}")

        return resolved_config

    def _get_float_config(self, key: str, default: float) -> float:
        if not self.config:
            return default
        value = self.config.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _get_int_config(self, key: str, default: int, minimum: Optional[int] = None) -> int:
        if not self.config:
            value = default
        else:
            candidate = self.config.get(key, default)
            try:
                value = int(candidate)
            except (TypeError, ValueError):
                value = default
        if minimum is not None and value < minimum:
            value = minimum
        return value

    def _get_bool_config(self, key: str, default: bool) -> bool:
        if not self.config:
            return default
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
        return bool(value)

    def _load_misfire_probability(self) -> float:
        probability = self._get_float_config('misfire_probability', DEFAULT_MISFIRE_PROBABILITY)
        if probability < 0:
            return 0.0
        if probability > 1:
            return 1.0
        return probability

    def _load_ban_duration_bounds(self) -> Tuple[int, int]:
        min_seconds = self._get_int_config('min_ban_seconds', DEFAULT_MIN_BAN_DURATION, minimum=1)
        max_seconds = self._get_int_config('max_ban_seconds', DEFAULT_MAX_BAN_DURATION, minimum=min_seconds)
        if max_seconds < min_seconds:
            max_seconds = min_seconds
        return min_seconds, max_seconds

    def _load_timeout_seconds(self) -> int:
        return self._get_int_config('timeout_seconds', DEFAULT_TIMEOUT_SECONDS, minimum=1)

    def _load_default_misfire_switch(self) -> bool:
        return self._get_bool_config('misfire_enabled_by_default', False)

    def _load_texts(self):
        """加载游戏文本，多编码尝试"""
        if self._cached_texts is not None:
            return self._cached_texts

        encodings = ['utf-8', 'gbk', 'gb2312']
        for encoding in encodings:
            try:
                with open(self.texts_file, 'r', encoding=encoding) as file:
                    loaded = yaml.safe_load(file) or {}
                    self._cached_texts = loaded
                    break
            except UnicodeDecodeError:
                continue
            except FileNotFoundError:
                break
        else:
            self._cached_texts = {}

        if not self._cached_texts:
            self._cached_texts = self._load_default_texts()
        self.texts = self._cached_texts
        return self._cached_texts

    def _load_default_texts(self) -> Dict[str, Any]:
        if self._default_texts is not None:
            return self._default_texts

        try:
            with open(DEFAULT_TEXTS_FILE, 'r', encoding='utf-8') as file:
                self._default_texts = yaml.safe_load(file) or {}
        except FileNotFoundError:
            self._default_texts = {key: value[:] for key, value in DEFAULT_FALLBACK_TEXTS.items()}
        return self._default_texts

    def _ensure_texts_file(self):
        if os.path.exists(self.texts_file):
            return
        default_texts = self._load_default_texts()
        if os.path.exists(DEFAULT_TEXTS_FILE):
            shutil.copy(DEFAULT_TEXTS_FILE, self.texts_file)
        else:
            default_texts = {key: value[:] for key, value in DEFAULT_FALLBACK_TEXTS.items()}
            with open(self.texts_file, 'w', encoding='utf-8') as file:
                yaml.dump(default_texts, file, allow_unicode=True)
        self._cached_texts = {
            key: (value[:] if isinstance(value, list) else value)
            for key, value in default_texts.items()
        }

    def _get_text_list(self, key: str) -> List[str]:
        texts = self._load_texts()
        values = texts.get(key)
        if not values or not isinstance(values, list):
            values = self._load_default_texts().get(key, [])
        return values if isinstance(values, list) else []

    def _choose_text(self, key: str) -> str:
        choices = self._get_text_list(key)
        if choices:
            return random.choice(choices)
        fallback_choices = DEFAULT_FALLBACK_TEXTS.get(key, [])
        return random.choice(fallback_choices) if fallback_choices else ""

    def _load_misfire_switches(self):
        """从配置文件加载走火开关信息"""
        texts = self._load_texts()
        switches = texts.get('misfire_switches', {}) or {}
        normalized: Dict[Any, bool] = {}
        for key, value in dict(switches).items():
            try:
                normalized[int(key)] = bool(value)
            except (TypeError, ValueError):
                normalized[key] = bool(value)
        return normalized

    def _save_misfire_switches(self):
        """保存走火开关信息到配置文件"""
        texts = self._load_texts()
        if 'misfire_switches' not in texts:
            texts['misfire_switches'] = {}
        texts['misfire_switches'].update(self.group_misfire_switches)
        with open(self.texts_file, 'w', encoding='utf-8') as file:
            yaml.dump(texts, file, allow_unicode=True)
        self._cached_texts = texts
        self.texts = texts

    @event_message_type(EventMessageType.ALL)
    async def on_all_messages(self, event: AstrMessageEvent, message: str = ""):
        """处理所有消息，仅用于检查随机走火"""
        group_id = self._get_group_id(event)
        is_private = not group_id  # 判断是否为私聊
        raw_message = message if message is not None else ""
        message_str = (raw_message or event.message_str or "").strip()

        if is_private:
            if self._is_registered_command(message_str):
                yield event.plain_result("该游戏仅限群聊中使用，请在群内游玩。")
            return

        self._init_group_misfire_switch(group_id)

        if self._is_registered_command(message_str):
            return

        if not self.group_misfire_switches[group_id]:
            return

        if random.random() <= self.misfire_probability:
            async for result in self._handle_misfire(event, group_id):
                yield result

    def _is_registered_command(self, message_str: str) -> bool:
        """判断消息是否为已注册的指令，避免和随机走火冲突"""
        if not message_str:
            return False
        normalized = message_str.lstrip('/').split()
        if not normalized:
            return False
        return normalized[0] in self.COMMAND_KEYWORDS

    @command("装填", aliases=["/装填"])
    async def command_load(self, event: AstrMessageEvent, message: str = ""):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该游戏仅限群聊中使用，请在群内游玩。")
            return
        self._init_group_misfire_switch(group_id)
        num_bullets = self._parse_bullet_count(message)
        if num_bullets is None:
            yield event.plain_result("你输入的装填子弹数量不是有效的整数，请重新输入。")
            return
        async for result in self.load_bullets(event, num_bullets):
            yield result

    @command("开枪", aliases=["/开枪"])
    async def command_shoot(self, event: AstrMessageEvent, message: str = ""):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该游戏仅限群聊中使用，请在群内游玩。")
            return
        self._init_group_misfire_switch(group_id)
        async for result in self.execute_shot(event):
            yield result

    @command("走火开", aliases=["/走火开"])
    async def command_misfire_on(self, event: AstrMessageEvent, message: str = ""):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该游戏仅限群聊中使用，请在群内游玩。")
            return
        self._init_group_misfire_switch(group_id)
        result = await self._handle_misfire_switch_on(event, group_id)
        yield result

    @command("走火关", aliases=["/走火关"])
    async def command_misfire_off(self, event: AstrMessageEvent, message: str = ""):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该游戏仅限群聊中使用，请在群内游玩。")
            return
        self._init_group_misfire_switch(group_id)
        result = await self._handle_misfire_switch_off(event, group_id)
        yield result

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[int]:
        """获取群id"""
        return event.message_obj.group_id if hasattr(event.message_obj, "group_id") else None

    def _init_group_misfire_switch(self, group_id):
        """初始化群走火开关"""
        if group_id not in self.group_misfire_switches:
            self.group_misfire_switches[group_id] = self.default_misfire_enabled

    async def _handle_misfire_switch_on(self, event: AstrMessageEvent, group_id):
        """开启群走火开关并保存信息"""
        self.group_misfire_switches[group_id] = True
        self._save_misfire_switches()
        return event.plain_result("本群左轮手枪走火功能已开启！")

    async def _handle_misfire_switch_off(self, event: AstrMessageEvent, group_id):
        """关闭群走火开关并保存信息"""
        self.group_misfire_switches[group_id] = False
        self._save_misfire_switches()
        return event.plain_result("本群左轮手枪走火功能已关闭！")

    async def _handle_misfire(self, event: AstrMessageEvent, group_id):
        """处理走火事件，禁言用户"""
        sender_nickname = event.get_sender_name()
        client = event.bot

        misfire_desc = self._choose_text('misfire_descriptions')
        user_reaction = self._choose_text('user_reactions').format(sender_nickname=sender_nickname)
        message = f"{misfire_desc} {user_reaction} 不幸被击中！".strip()
        try:
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"Failed to handle misfire: {e}")
            await self.context.send_message(event.unified_msg_origin, "消息发送失败，请稍后重试。")
        await self._ban_user(event, client, int(event.get_sender_id()))

    def _parse_bullet_count(self, argument_text: str):
        """解析装填子弹数量，默认装填一发"""
        argument_text = (argument_text or "").strip()
        if not argument_text:
            return 1
        first_segment = argument_text.split()[0]
        try:
            return int(first_segment)
        except ValueError:
            return None

    async def load_bullets(self, event: AstrMessageEvent, x: int = 1):
        """装填子弹，检查并启动定时器"""
        sender_nickname = event.get_sender_name()
        group_id = event.message_obj.group_id
        group_state = self.group_states.get(group_id)

        job_id = f"timeout_{group_id}"
        self._remove_timer_job(job_id)

        if group_state and 'chambers' in group_state and any(group_state['chambers']):
            yield event.plain_result(f"{sender_nickname}，游戏还未结束，不能重新装填，请继续射击！")
            return

        if x < 1 or x > CHAMBER_COUNT:
            yield event.plain_result(f"{sender_nickname}，装填的实弹数量必须在 1 到 6 之间，请重新输入。")
            return

        chambers = [False] * CHAMBER_COUNT
        positions = random.sample(range(CHAMBER_COUNT), x)
        for pos in positions:
            chambers[pos] = True

        group_state = {
            'chambers': chambers,
            'current_chamber_index': 0
        }
        self.group_states[group_id] = group_state

        load_message = (
            f"{sender_nickname} 装填了 {x} 发实弹到 {CHAMBER_COUNT} 弹膛的左轮手枪，"
            f"输入 /开枪 在 {self.timeout_seconds} 秒内开始游戏！"
        )
        yield event.plain_result(load_message)
        self.start_timer(event, group_id, self.timeout_seconds)

    async def execute_shot(self, event: AstrMessageEvent):
        """射击操作，处理结果"""
        sender_nickname = event.get_sender_name()
        group_id = event.message_obj.group_id
        group_state = self.group_states.get(group_id)

        job_id = f"timeout_{group_id}"
        self._remove_timer_job(job_id)

        if not group_state or 'chambers' not in group_state:
            yield event.plain_result(f"{sender_nickname}，枪里好像没有子弹呢，请先装填。")
            return

        client = event.bot
        self.start_timer(event, group_id, self.timeout_seconds)

        chambers = group_state['chambers']
        current_index = group_state['current_chamber_index']

        if chambers[current_index]:
            async for result in self._handle_real_shot(event, group_state, chambers, current_index, sender_nickname, client):
                yield result
        else:
            async for result in self._handle_empty_shot(event, group_state, chambers, current_index, sender_nickname):
                yield result

        remaining_bullets = sum(group_state['chambers'])
        if remaining_bullets == 0:
            self._remove_timer_job(job_id)
            del self.group_states[group_id]
            yield event.plain_result(f"{sender_nickname}，弹匣内的所有实弹都已射出，游戏结束。若想继续，可再次装填。")

    async def _handle_real_shot(self, event: AstrMessageEvent, group_state, chambers, current_index, sender_nickname, client):
        """处理击中目标，更新状态并禁言用户"""
        chambers[current_index] = False
        group_state['current_chamber_index'] = (current_index + 1) % CHAMBER_COUNT
        trigger_desc = self._choose_text('trigger_descriptions')
        user_reaction = self._choose_text('user_reactions').format(sender_nickname=sender_nickname)
        message = f"{trigger_desc}，{user_reaction}".strip('，').strip()
        try:
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"Failed to handle real shot: {e}")
            await self.context.send_message(event.unified_msg_origin, "消息发送失败，请稍后重试。")
        await self._ban_user(event, client, int(event.get_sender_id()))

    async def _handle_empty_shot(self, event: AstrMessageEvent, group_state, chambers, current_index, sender_nickname):
        """处理未击中目标，更新状态"""
        group_state['current_chamber_index'] = (current_index + 1) % CHAMBER_COUNT
        miss_message = self._choose_text('miss_messages').format(sender_nickname=sender_nickname).strip()
        try:
            yield event.plain_result(miss_message)
        except Exception as e:
            logger.error(f"Failed to handle empty shot: {e}")
            await self.context.send_message(event.unified_msg_origin, "消息发送失败，请稍后重试。")

    def start_timer(self, event: AstrMessageEvent, group_id, seconds):
        """启动群定时器"""
        umo = event.unified_msg_origin
        self.group_umo_mapping[group_id] = umo

        run_time = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
        job_id = f"timeout_{group_id}"
        self.scheduler.add_job(
            self.timeout_callback,
            'date',
            run_date=run_time,
            args=[group_id],
            id=job_id,
            replace_existing=True
        )

    async def timeout_callback(self, group_id):
        """定时器超时，移除群游戏状态"""
        umo = self.group_umo_mapping.pop(group_id, None)
        if group_id in self.group_states:
            del self.group_states[group_id]
        if not umo:
            return
        timeout_message = "长时间未操作，当前左轮手枪游戏已自动结束。"
        try:
            await self.context.send_message(umo, timeout_message)
        except Exception as e:
            logger.error(f"Failed to send timeout message: {e}")

    async def _ban_user(self, event: AstrMessageEvent, client, user_id):
        """禁言用户"""
        ban_method = getattr(client, 'set_group_ban', None)
        ban_duration = random.randint(self.min_ban_seconds, self.max_ban_seconds)
        if not callable(ban_method):
            await self.context.send_message(event.unified_msg_origin, "机器人缺少禁言能力，无法执行左轮手枪惩罚。")
            return

        try:
            await ban_method(
                group_id=int(event.get_group_id()),
                user_id=user_id,
                duration=ban_duration,
                self_id=int(event.get_self_id())
            )
        except PermissionError:
            await self.context.send_message(event.unified_msg_origin, "机器人权限不足，无法禁言该成员。")
        except Exception as e:
            logger.error(f"Failed to ban user: {e}")
            await self.context.send_message(event.unified_msg_origin, "禁言失败，请检查机器人权限或稍后再试。")

    def _remove_timer_job(self, job_id):
        """移除定时器任务"""
        try:
            self.scheduler.remove_job(job_id)
        except JobLookupError:
            return
        except Exception as e:
            logger.error(f"Failed to remove timer job: {e}")

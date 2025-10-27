import random
import yaml
import os
import datetime
import shutil
from typing import Any, Dict, List

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.all import (
    AstrMessageEvent,
    Context,
    EventMessageType,
    Star,
    event_message_type,
    register,
)
from astrbot.api import logger

# 插件目录
PLUGIN_DIR = os.path.join('data', 'plugins', 'astrbot_plugin_rg')
# 确保插件目录存在
if not os.path.exists(PLUGIN_DIR):
    os.makedirs(PLUGIN_DIR)

# 配置路径
TEXTS_FILE = os.path.join(PLUGIN_DIR, 'revolver_game_texts.yml')
DEFAULT_TEXTS_FILE = os.path.join(os.path.dirname(__file__), 'revolver_game_texts.yml')

DEFAULT_FALLBACK_TEXTS: Dict[str, str] = {
    'misfire_descriptions': '手枪突然走火。',
    'user_reactions': '{sender_nickname} 吓得不轻。',
    'trigger_descriptions': '扳机被扣下。',
    'miss_messages': '{sender_nickname} 幸运地打出了一发空弹。'
}

@register("astrbot_plugin_rg", "zgojin, piexian", "1.4.0", "https://github.com/piexian/astrbot_plugin_rg")
class RevolverGamePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 从 context 中获取配置
        self.config = context.get_config()
        # 群游戏状态
        self.group_states: Dict[int, Dict[str, Any]] = {}
        # 加载走火开关
        self.group_misfire_switches = self._load_misfire_switches()
        # 走火概率
        self.misfire_probability = 0.005
        # 加载游戏文本
        self.texts = self._load_texts()
        # 初始化定时器调度器
        if not hasattr(context, 'scheduler'):
            context.scheduler = AsyncIOScheduler()
            context.scheduler.start()
        self.scheduler = context.scheduler
        # 群消息来源映射
        self.group_umo_mapping: Dict[int, Any] = {}

    def _load_texts(self):
        """加载游戏文本，多编码尝试"""
        if not hasattr(self, '_cached_texts'):
            self._ensure_texts_file()
            encodings = ['utf-8', 'gbk', 'gb2312']
            for encoding in encodings:
                try:
                    with open(TEXTS_FILE, 'r', encoding=encoding) as file:
                        loaded = yaml.safe_load(file) or {}
                        self._cached_texts = loaded
                        break
                except UnicodeDecodeError:
                    continue
                except FileNotFoundError:
                    break
            else:
                self._cached_texts = {}

            if not getattr(self, '_cached_texts', None):
                self._cached_texts = self._load_default_texts()
        return self._cached_texts

    def _load_default_texts(self) -> Dict[str, Any]:
        if not hasattr(self, '_default_texts'):
            try:
                with open(DEFAULT_TEXTS_FILE, 'r', encoding='utf-8') as file:
                    self._default_texts = yaml.safe_load(file) or {}
            except FileNotFoundError:
                self._default_texts = {}
        return self._default_texts

    def _ensure_texts_file(self):
        if os.path.exists(TEXTS_FILE):
            return
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        default_texts = self._load_default_texts()
        if os.path.exists(DEFAULT_TEXTS_FILE):
            shutil.copy(DEFAULT_TEXTS_FILE, TEXTS_FILE)
        else:
            with open(TEXTS_FILE, 'w', encoding='utf-8') as file:
                yaml.dump(default_texts, file, allow_unicode=True)
        self._cached_texts = default_texts.copy()

    def _get_text_list(self, key: str) -> List[str]:
        texts = self._load_texts()
        values = texts.get(key)
        if not values:
            values = self._load_default_texts().get(key, [])
        return values or []

    def _choose_text(self, key: str) -> str:
        choices = self._get_text_list(key)
        if choices:
            return random.choice(choices)
        return DEFAULT_FALLBACK_TEXTS.get(key, "")

    def _load_misfire_switches(self):
        """从配置文件加载走火开关信息"""
        texts = self._load_texts()
        return texts.get('misfire_switches', {}) or {}

    def _save_misfire_switches(self):
        """保存走火开关信息到配置文件"""
        texts = self._load_texts()
        if 'misfire_switches' not in texts:
            texts['misfire_switches'] = {}
        texts['misfire_switches'].update(self.group_misfire_switches)
        with open(TEXTS_FILE, 'w', encoding='utf-8') as file:
            yaml.dump(texts, file, allow_unicode=True)
        self._cached_texts = texts

    @event_message_type(EventMessageType.ALL)
    async def on_all_messages(self, event: AstrMessageEvent):
        """处理所有消息"""
        group_id = self._get_group_id(event)
        is_private = not group_id  # 判断是否为私聊
        message_str = event.message_str.strip()
        
        if is_private:
            valid_commands = ["走火开", "走火关", "装填", "开枪"]
            if any(message_str.startswith(cmd) for cmd in valid_commands):
                yield event.plain_result("该游戏仅限群聊中使用，请在群内游玩。")
            # 直接返回，不对私聊消息进行任何其他处理
            return

        self._init_group_misfire_switch(group_id)

        if message_str == "走火开":
            result = await self._handle_misfire_switch_on(event, group_id)
            yield result
        elif message_str == "走火关":
            result = await self._handle_misfire_switch_off(event, group_id)
            yield result
        elif not self.group_misfire_switches[group_id]:
            pass
        else:
            if random.random() <= self.misfire_probability:
                async for result in self._handle_misfire(event, group_id):
                    yield result

        if message_str.startswith("装填"):
            num_bullets = self._parse_bullet_count(message_str)
            if num_bullets is None:
                yield event.plain_result("你输入的装填子弹数量不是有效的整数，请重新输入。")
            else:
                async for result in self.load_bullets(event, num_bullets):
                    yield result
        elif message_str == "开枪":
            async for result in self.shoot(event):
                yield result

    def _get_group_id(self, event: AstrMessageEvent):
        """获取群id"""
        return event.message_obj.group_id if hasattr(event.message_obj, "group_id") else None

    def _init_group_misfire_switch(self, group_id):
        """初始化群走火开关"""
        if group_id not in self.group_misfire_switches:
            self.group_misfire_switches[group_id] = False

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
        await self._ban_user(event, client, int(event.get_sender_id()))

    def _parse_bullet_count(self, message_str):
        """解析装填子弹数量"""
        parts = message_str.split()
        if len(parts) > 1:
            try:
                return int(parts[1])
            except ValueError:
                return None
        return 1

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

        if x < 1 or x > 6:
            yield event.plain_result(f"{sender_nickname}，装填的实弹数量必须在 1 到 6 之间，请重新输入。")
            return

        chambers = [False] * 6
        positions = random.sample(range(6), x)
        for pos in positions:
            chambers[pos] = True

        group_state = {
            'chambers': chambers,
            'current_chamber_index': 0
        }
        self.group_states[group_id] = group_state

        yield event.plain_result(f"{sender_nickname} 装填了 {x} 发实弹到 6 弹匣的左轮手枪，输入 /开枪 开始游戏！")
        self.start_timer(event, group_id, 180)

    async def shoot(self, event: AstrMessageEvent):
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
        self.start_timer(event, group_id, 180)

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
        group_state['current_chamber_index'] = (current_index + 1) % 6
        trigger_desc = self._choose_text('trigger_descriptions')
        user_reaction = self._choose_text('user_reactions').format(sender_nickname=sender_nickname)
        message = f"{trigger_desc}，{user_reaction}".strip('，').strip()
        try:
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"Failed to handle real shot: {e}")
        await self._ban_user(event, client, int(event.get_sender_id()))

    async def _handle_empty_shot(self, event: AstrMessageEvent, group_state, chambers, current_index, sender_nickname):
        """处理未击中目标，更新状态"""
        group_state['current_chamber_index'] = (current_index + 1) % 6
        miss_message = self._choose_text('miss_messages').format(sender_nickname=sender_nickname).strip()
        try:
            yield event.plain_result(miss_message)
        except Exception as e:
            logger.error(f"Failed to handle empty shot: {e}")

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
        if group_id in self.group_states:
            del self.group_states[group_id]
        umo = self.group_umo_mapping.pop(group_id, None)
        if not umo:
            return
        timeout_message = "长时间未操作，当前左轮手枪游戏已自动结束。"
        try:
            await self.context.send_message(umo, timeout_message)
        except Exception as e:
            logger.error(f"Failed to send timeout message: {e}")

    async def _ban_user(self, event: AstrMessageEvent, client, user_id):
        """禁言用户"""
        try:
            await client.set_group_ban(
                group_id=int(event.get_group_id()),
                user_id=user_id,
                duration=random.randint(60, 3000),  # 修改括号内的数字即可改变随机时间
                self_id=int(event.get_self_id())
            )
        except Exception as e:
            logger.error(f"Failed to ban user: {e}")

    def _remove_timer_job(self, job_id):
        """移除定时器任务"""
        try:
            self.scheduler.remove_job(job_id)
        except JobLookupError:
            return
        except Exception as e:
            logger.error(f"Failed to remove timer job: {e}")

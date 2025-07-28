import asyncio
import random
import yaml
import os
import datetime
from astrbot.api.all import *
from astrbot.api.event import MessageChain
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger
import astrbot.api.message_components as Comp

# 插件目录
PLUGIN_DIR = os.path.join('data', 'plugins', 'astrbot_plugin_rg')
# 确保插件目录存在
if not os.path.exists(PLUGIN_DIR):
    os.makedirs(PLUGIN_DIR)

# 配置路径
TEXTS_FILE = os.path.join(PLUGIN_DIR, 'revolver_game_texts.yml')

@register("astrbot_plugin_rg", "zgojin, piexian", "1.4.1", "https://github.com/piexian/astrbot_plugin_rg",config=True )
class RevolverGamePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 使用配置
        self.config = {
            'misfire_probability': config.get('misfire_probability', 0.005),
            'game_timeout': config.get('game_timeout', 180),
            'ban_duration_min': config.get('ban_duration', {}).get('min', 60),
            'ban_duration_max': config.get('ban_duration', {}).get('max', 3000),
            'max_bullets': config.get('max_bullets', 6)
        }
        # 从 context 中获取配置
        self.context = context
        # 群游戏状态
        self.group_states = {}
        # 加载走火开关
        self.group_misfire_switches = self._load_misfire_switches()
        # 群定时器开始时间
        self.group_timer_start_time = {}
        # 加载游戏文本
        self.texts = self._load_texts()
        # 初始化定时器调度器
        if not hasattr(context, 'scheduler'):
            context.scheduler = AsyncIOScheduler()
            context.scheduler.start()
        self.scheduler = context.scheduler
        # 群消息来源映射
        self.group_umo_mapping = {}

    def _load_texts(self):
        """加载游戏文本，多编码尝试"""
        if not hasattr(self, '_cached_texts'):
            encodings = ['utf-8', 'gbk', 'gb2312']
            for encoding in encodings:
                try:
                    with open(TEXTS_FILE, 'r', encoding=encoding) as file:
                        self._cached_texts = yaml.safe_load(file)
                        break
                except UnicodeDecodeError:
                    continue
            else:
                self._cached_texts = {}
        return self._cached_texts

    def _load_misfire_switches(self):
        """从配置文件加载走火开关信息"""
        texts = self._load_texts()
        return texts.get('misfire_switches', {})

    def _save_misfire_switches(self):
        """保存走火开关信息到配置文件"""
        texts = self._load_texts()
        if 'misfire_switches' not in texts:
            texts['misfire_switches'] = {}
        texts['misfire_switches'].update(self.group_misfire_switches)
        with open(TEXTS_FILE, 'w', encoding='utf-8') as file:
            yaml.dump(texts, file, allow_unicode=True)

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
            if random.random() <= self.config['misfire_probability']:
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

        misfire_desc = random.choice(self.texts.get('misfire_descriptions', []))
        user_reaction = random.choice(self.texts.get('user_reactions', [])).format(sender_nickname=sender_nickname)
        message = f"{misfire_desc} {user_reaction} 不幸被击中！"
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
            yield event.plain_result(f" 当前游戏还未结束，请先完成当前游戏。")
            return

        if x < 1 or x > self.config['max_bullets']:
            yield event.plain_result(f" 装填的实弹数量必须在 1 到 {self.config['max_bullets']} 之间，请重新输入。")
            return

        chambers = [False] * self.config['max_bullets']
        positions = random.sample(range(self.config['max_bullets']), x)
        for pos in positions:
            chambers[pos] = True

        group_state = {
            'chambers': chambers,
            'current_chamber_index': 0
        }
        self.group_states[group_id] = group_state

        yield event.plain_result(f" 装填了 {x} 发实弹到 {self.config['max_bullets']} 弹匣的左轮手枪，输入 /开枪 开始游戏！")
        self.start_timer(event, group_id, self.config['game_timeout'])

    async def shoot(self, event: AstrMessageEvent):
        """射击操作，处理结果"""
        sender_nickname = event.get_sender_name()
        group_id = event.message_obj.group_id
        group_state = self.group_states.get(group_id)

        job_id = f"timeout_{group_id}"
        self._remove_timer_job(job_id)

        if not group_state or 'chambers' not in group_state:
            yield event.plain_result(f" 枪里好像没有子弹呢，请先装填。")
            return

        client = event.bot
        self.start_timer(event, group_id, self.config['game_timeout'])

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
            yield event.plain_result(f" 弹匣内的所有实弹都已射出，游戏结束。若想继续，可再次 /装填。")

    async def _handle_real_shot(self, event: AstrMessageEvent, group_state, chambers, current_index, sender_nickname, client):
        """处理击中目标，更新状态并禁言用户"""
        chambers[current_index] = False
        group_state['current_chamber_index'] = (current_index + 1) % self.config['max_bullets']
        trigger_desc = random.choice(self.texts.get('trigger_descriptions', []))
        user_reaction = random.choice(self.texts.get('user_reactions', [])).format(sender_nickname=sender_nickname)
        message = f"{trigger_desc}，{user_reaction}"
        try:
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"Failed to handle real shot: {e}")
        await self._ban_user(event, client, int(event.get_sender_id()))

    async def _handle_empty_shot(self, event: AstrMessageEvent, group_state, chambers, current_index, sender_nickname):
        """处理未击中目标，更新状态"""
        group_state['current_chamber_index'] = (current_index + 1) % self.config['max_bullets']
        miss_message = random.choice(self.texts.get('miss_messages', [])).format(sender_nickname=sender_nickname)
        try:
            yield event.plain_result(miss_message)
        except Exception as e:
            logger.error(f"Failed to handle empty shot: {e}")


    async def timeout_callback(self, group_id, event: AstrMessageEvent):
        """定时器超时，移除群游戏状态"""
        if group_id in self.group_states:
            del self.group_states[group_id]
            group_state = self.group_states.get(group_id)
            if not group_state or 'chambers' not in group_state or not any(group_state['chambers']):
                yield event.plain_result(f" 游戏已经结束，请重新 /装填 开始游戏。")
                return

    async def _ban_user(self, event: AstrMessageEvent, client, user_id):
        """禁言用户"""
        try:
            await client.set_group_ban(
                group_id=int(event.get_group_id()),
                user_id=user_id,
                duration=random.randint(self.config['ban_duration_min'], self.config['ban_duration_max']), 
            )
        except Exception as e:
            logger.error(f"Failed to ban user: {e}")

    def _remove_timer_job(self, job_id):
        """移除定时器任务"""
        try:
            self.scheduler.remove_job(job_id)
        except Exception as e:
            logger.debug(f"Timer job {job_id} not found or already removed")

    def start_timer(self, event: AstrMessageEvent, group_id: int, timeout: int):
        """启动定时器"""
        job_id = f"timeout_{group_id}"
        try:
            self.scheduler.add_job(
                self.timeout_callback,
                'date',
                run_date=datetime.datetime.now() + datetime.timedelta(seconds=timeout),
                args=[group_id, event],
                id=job_id,
                replace_existing=True
            )
            logger.debug(f"Started timer for group {group_id} with timeout {timeout}s")
        except Exception as e:
            logger.error(f"Failed to start timer: {e}")

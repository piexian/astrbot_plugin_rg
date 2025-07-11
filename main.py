import asyncio
from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent  # 移除 EventMessageType 导入
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import datetime
import yaml
import random
import os

@register(
    "astrbot_plugin_rg", 
    "原作者zgojin", 
    "群聊左轮手枪游戏插件，支持随机走火事件（全指令可自定义）", 
    "1.7.0", 
    "https://github.com/piexian/astrbot_plugin_rg"
)
class RevolverGamePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 加载插件配置（控制台可修改）
        self.plugin_config = config
        
        # 核心配置：指令前缀和具体指令文本
        self.command_prefix = self.plugin_config.get("command_prefix", "/").strip()
        self.command_load = self.plugin_config.get("command_load", "装填").strip()
        self.command_shoot = self.plugin_config.get("command_shoot", "开枪").strip()
        self.command_misfire_on = self.plugin_config.get("command_misfire_on", "走火开").strip()
        self.command_misfire_off = self.plugin_config.get("command_misfire_off", "走火关").strip()
        
        # 其他配置项
        self.misfire_probability = self.plugin_config.get("misfire_probability", 0.005)
        self.default_misfire_switch = self.plugin_config.get("default_misfire_switch", False)
        self.ban_time_min = self.plugin_config.get("ban_time_range", {}).get("min", 60)
        self.ban_time_max = self.plugin_config.get("ban_time_range", {}).get("max", 3000)
        self.game_timeout = self.plugin_config.get("game_timeout", 180)
        
        # 初始化游戏状态
        self.group_states = {}
        self.group_misfire_switches = self._load_misfire_switches()
        self.group_timer_start_time = {}
        self.texts = self._load_texts()
        
        # 初始化定时器
        if not hasattr(context, 'scheduler'):
            context.scheduler = AsyncIOScheduler()
            context.scheduler.start()
        self.scheduler = context.scheduler
        
        # 数据目录（符合BOT规范）
        self.plugin_data_dir = StarTools.get_data_dir()
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.texts_file = self.plugin_data_dir / 'revolver_game_texts.yml'

    def _load_texts(self):
        """加载游戏文本"""
        if not hasattr(self, '_cached_texts'):
            encodings = ['utf-8', 'gbk', 'gb2312']
            for encoding in encodings:
                try:
                    with open(self.texts_file, 'r', encoding=encoding) as file:
                        self._cached_texts = yaml.safe_load(file)
                        break
                except (UnicodeDecodeError, FileNotFoundError):
                    continue
            else:
                self._cached_texts = {}
        return self._cached_texts

    def _load_misfire_switches(self):
        """从配置文件加载走火开关状态"""
        texts = self._load_texts()
        return texts.get('misfire_switches', {})

    def _save_misfire_switches(self):
        """保存走火开关状态到文件"""
        texts = self._load_texts()
        if 'misfire_switches' not in texts:
            texts['misfire_switches'] = {}
        texts['misfire_switches'].update(self.group_misfire_switches)
        with open(self.texts_file, 'w', encoding='utf-8') as file:
            yaml.dump(texts, file, allow_unicode=True)

    def _get_full_command(self, command: str) -> str:
        """生成带前缀的完整指令"""
        return f"{self.command_prefix}{command}" if self.command_prefix else command

    def _is_command(self, message_str: str, command: str) -> bool:
        """判断消息是否为指定指令（带前缀）"""
        return message_str.strip() == self._get_full_command(command)

    def _strip_prefix(self, message_str: str) -> str:
        """移除消息中的指令前缀"""
        if self.command_prefix and message_str.startswith(self.command_prefix):
            return message_str[len(self.command_prefix):].strip()
        return message_str

    @filter()  # 移除 EventMessageType.ALL，使用默认过滤（监听所有消息）
    async def on_all_messages(self, event: AstrMessageEvent):
        """处理所有消息（使用控制台配置的指令）"""
        group_id = self._get_group_id(event)
        is_private = not group_id
        message_str = event.message_str.strip()
        
        # 私聊提示群聊可用
        if is_private:
            valid_commands = [
                self._get_full_command(self.command_misfire_on),
                self._get_full_command(self.command_misfire_off),
                self._get_full_command(self.command_load),
                self._get_full_command(self.command_shoot)
            ]
            if any(message_str.startswith(cmd) for cmd in valid_commands):
                yield event.plain_result("该游戏仅限群聊中使用，请在群内游玩。")
            return
        
        self._init_group_misfire_switch(group_id)
        
        # 处理走火开关指令
        if self._is_command(message_str, self.command_misfire_on):
            yield await self._handle_misfire_switch_on(event, group_id)
        elif self._is_command(message_str, self.command_misfire_off):
            yield await self._handle_misfire_switch_off(event, group_id)
        
        # 处理装填指令（带参数）
        elif (self.command_prefix and message_str.startswith(self._get_full_command(self.command_load))) or \
             (not self.command_prefix and message_str.startswith(self.command_load)):
            cleaned_msg = self._strip_prefix(message_str)
            num_bullets = self._parse_bullet_count(cleaned_msg)
            async for result in self.handle_load(event, num_bullets):
                yield result
        
        # 处理射击指令
        elif self._is_command(message_str, self.command_shoot):
            async for result in self.handle_shoot(event):
                yield result
        
        # 走火功能开启时，随机触发走火事件
        if self.group_misfire_switches[group_id] and random.random() <= self.misfire_probability:
            async for result in self._handle_misfire(event, group_id):
                yield result

    async def handle_load(self, event: AstrMessageEvent, num_bullets: int = 1):
        """处理装填指令"""
        group_id = self._get_group_id(event)
        if not group_id:
            return
        sender_nickname = event.get_sender_name()
        full_load_command = self._get_full_command(self.command_load)
        
        if num_bullets is None:
            yield event.plain_result(
                f"你输入的装填子弹数量不是有效的整数，请重新输入（例如：{full_load_command} 3）。"
            )
            return
        
        group_state = self.group_states.get(group_id)
        if group_state and 'chambers' in group_state and any(group_state['chambers']):
            yield event.plain_result(f"{sender_nickname}，游戏还未结束，不能重新装填，请继续射击！")
            return
        
        if num_bullets < 1 or num_bullets > 6:
            yield event.plain_result(
                f"{sender_nickname}，装填的实弹数量必须在 1 到 6 之间，请重新输入（例如：{full_load_command} 3）。"
            )
            return
        
        # 初始化弹匣
        chambers = [False] * 6
        positions = random.sample(range(6), num_bullets)
        for pos in positions:
            chambers[pos] = True
        self.group_states[group_id] = {
            'chambers': chambers,
            'current_chamber_index': 0
        }
        
        yield event.plain_result(f"{sender_nickname} 装填了 {num_bullets} 发实弹到 6 弹匣的左轮手枪，游戏开始！")
        self.start_timer(event, group_id, self.game_timeout)

    async def handle_shoot(self, event: AstrMessageEvent):
        """处理射击指令"""
        group_id = self._get_group_id(event)
        if not group_id:
            return
        sender_nickname = event.get_sender_name()
        group_state = self.group_states.get(group_id)
        job_id = f"timeout_{group_id}"
        
        self._remove_timer_job(job_id)
        
        if not group_state or 'chambers' not in group_state:
            yield event.plain_result(
                f"{sender_nickname}，枪里好像没有子弹呢，请先使用 {self._get_full_command(self.command_load)} 指令。"
            )
            return
        
        self.start_timer(event, group_id, self.game_timeout)
        chambers = group_state['chambers']
        current_index = group_state['current_chamber_index']
        
        if chambers[current_index]:
            async for result in self._handle_real_shot(event, group_state, chambers, current_index, sender_nickname, event.bot):
                yield result
        else:
            async for result in self._handle_empty_shot(event, group_state, chambers, current_index, sender_nickname):
                yield result
        
        remaining_bullets = sum(group_state['chambers'])
        if remaining_bullets == 0:
            self._remove_timer_job(job_id)
            del self.group_states[group_id]
            yield event.plain_result(
                f"{sender_nickname}，弹匣内的所有实弹都已射出，游戏结束。若想继续，可再次使用 {self._get_full_command(self.command_load)} 指令。"
            )

    def _get_group_id(self, event: AstrMessageEvent):
        """获取群ID"""
        return event.message_obj.group_id if hasattr(event.message_obj, "group_id") else None

    def _init_group_misfire_switch(self, group_id):
        """初始化群走火开关"""
        if group_id not in self.group_misfire_switches:
            self.group_misfire_switches[group_id] = self.default_misfire_switch

    async def _handle_misfire_switch_on(self, event: AstrMessageEvent, group_id):
        """开启走火功能"""
        self.group_misfire_switches[group_id] = True
        self._save_misfire_switches()
        return event.plain_result(f"本群左轮手枪走火功能已开启！使用 {self._get_full_command(self.command_misfire_off)} 可关闭。")

    async def _handle_misfire_switch_off(self, event: AstrMessageEvent, group_id):
        """关闭走火功能"""
        self.group_misfire_switches[group_id] = False
        self._save_misfire_switches()
        return event.plain_result(f"本群左轮手枪走火功能已关闭！使用 {self._get_full_command(self.command_misfire_on)} 可开启。")

    async def _handle_misfire(self, event: AstrMessageEvent, group_id):
        """处理随机走火事件"""
        sender_nickname = event.get_sender_name()
        client = event.bot
        misfire_desc = random.choice(self.texts.get('misfire_descriptions', ["突然，左轮手枪走火了！"]))
        user_reaction = random.choice(self.texts.get('user_reactions', ["{sender_nickname}被流弹击中"])).format(sender_nickname=sender_nickname)
        message = f"{misfire_desc} {user_reaction} 不幸被击中！"
        yield event.plain_result(message)
        await self._ban_user(event, client, int(event.get_sender_id()))

    def _parse_bullet_count(self, message_str: str):
        """解析子弹数量"""
        parts = message_str.split()
        if len(parts) >= 2 and parts[0] == self.command_load:
            try:
                return int(parts[1])
            except ValueError:
                return None
        return 1

    def start_timer(self, event: AstrMessageEvent, group_id, seconds):
        """启动超时定时器"""
        job_id = f"timeout_{group_id}"
        self.scheduler.add_job(
            self.timeout_callback,
            'date',
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=seconds),
            args=[group_id],
            id=job_id
        )

    async def timeout_callback(self, group_id):
        """超时回调"""
        if group_id in self.group_states:
            del self.group_states[group_id]

    async def _ban_user(self, event: AstrMessageEvent, client, user_id):
        """禁言用户"""
        random_duration = random.randint(self.ban_time_min, self.ban_time_max)
        try:
            await client.set_group_ban(
                group_id=int(event.get_group_id()),
                user_id=user_id,
                duration=random_duration,
                self_id=int(event.get_self_id())
            )
        except Exception as e:
            logger.error(f"禁言操作失败: {e}")

    def _remove_timer_job(self, job_id):
        """移除定时器任务"""
        try:
            self.scheduler.remove_job(job_id)
        except Exception as e:
            logger.error(f"移除定时器失败: {e}")

    async def _handle_real_shot(self, event: AstrMessageEvent, group_state, chambers, current_index, sender_nickname, client):
        """处理实弹射击"""
        chambers[current_index] = False
        group_state['current_chamber_index'] = (current_index + 1) % 6
        trigger_desc = random.choice(self.texts.get('trigger_descriptions', ["枪响了"]))
        user_reaction = random.choice(self.texts.get('user_reactions', ["{sender_nickname}被击中了"])).format(sender_nickname=sender_nickname)
        message = f"{trigger_desc}，{user_reaction}"
        yield event.plain_result(message)
        await self._ban_user(event, client, int(event.get_sender_id()))

    async def _handle_empty_shot(self, event: AstrMessageEvent, group_state, chambers, current_index, sender_nickname):
        """处理空枪射击"""
        group_state['current_chamber_index'] = (current_index + 1) % 6
        miss_message = random.choice(self.texts.get('miss_messages', ["是空枪！{sender_nickname}安全了"])).format(sender_nickname=sender_nickname)
        yield event.plain_result(miss_message)

    async def terminate(self):
        """插件卸载清理"""
        if hasattr(self, 'scheduler'):
            self.scheduler.shutdown()
        logger.info("左轮手枪游戏插件已卸载")

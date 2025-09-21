"""
AstrBot Plugin: Revolver Game (Russian Roulette)
- A modernized version of the classic revolver game plugin, compliant with the latest official docs.
- Author: zgojin, piexian
- Refactored by: Roo
"""

import asyncio
import random
import yaml
import os
import datetime
import json

# Import modern AstrBot APIs according to the latest official documentation
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.utils import get_astrbot_data_path
from astrbot.api import logger
import astrbot.api.message_components as Comp

# Define the path for plugin's persistent data
DATA_DIR = os.path.join(get_astrbot_data_path(), 'astrbot_plugin_rg')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
RUNTIME_DATA_FILE = os.path.join(DATA_DIR, 'data.json')

@register("astrbot_plugin_rg", "zgojin, piexian (Refactored by Roo)", "2.0.2", "A modernized revolver game plugin with GUI config.")
class RevolverGame(Star):
    """
    A Russian Roulette game plugin where players can load a revolver and take turns shooting.
    Features a passive "misfire" mechanic that can be enabled on a per-group basis.
    """
    def __init__(self, context: Context):
        """
        Initializes the plugin instance.
        """
        self.config = context.get_config()
        self.texts = {}
        self.default_texts = {}
        
        self.misfire_probability = 0.005
        self.min_ban_duration = 60
        self.max_ban_duration = 3000

        self.group_states = {}
        self.group_misfire_switches = {}

        self._load_default_texts()
        self._process_custom_texts()
        self._load_game_settings()
        self._load_runtime_data()

        self.scheduler = context.get_scheduler()
        logger.info("Revolver Game plugin loaded successfully.")

    def _load_default_texts(self):
        """Loads the default game texts from the YAML file."""
        try:
            current_dir = os.path.dirname(__file__)
            texts_file = os.path.join(current_dir, 'revolver_game_texts.yml')
            with open(texts_file, 'r', encoding='utf-8') as f:
                self.default_texts = yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load default texts: {e}")
            self.default_texts = {
                'misfire_descriptions': ['The gun misfired!'],
                'user_reactions': ['{sender_nickname} was hit!'],
                'trigger_descriptions': ['BANG!'],
                'miss_messages': ['Click. An empty chamber.']
            }

    def _process_custom_texts(self):
        """Processes custom texts from the config, with fallback to defaults."""
        custom_texts_config = self.config.get('custom_texts', {})
        for key, default_value in self.default_texts.items():
            if key == 'misfire_switches':
                continue
            user_text = custom_texts_config.get(key, "").strip()
            if user_text:
                self.texts[key] = user_text.splitlines()
            else:
                self.texts[key] = default_value

    def _load_game_settings(self):
        """Loads core game parameters from the config."""
        game_settings = self.config.get('game_settings', {})
        self.misfire_probability = game_settings.get('misfire_probability', 0.005)
        self.min_ban_duration = game_settings.get('min_ban_duration', 60)
        self.max_ban_duration = game_settings.get('max_ban_duration', 3000)

    def _load_runtime_data(self):
        """Loads persistent runtime data from the data.json file."""
        try:
            if os.path.exists(RUNTIME_DATA_FILE):
                with open(RUNTIME_DATA_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.group_misfire_switches = data.get('group_misfire_switches', {})
        except Exception as e:
            logger.error(f"Failed to load runtime data: {e}")
            self.group_misfire_switches = {}

    def _save_runtime_data(self):
        """Saves persistent runtime data to the data.json file."""
        try:
            data = {'group_misfire_switches': self.group_misfire_switches}
            with open(RUNTIME_DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Failed to save runtime data: {e}")

    def terminate(self):
        """Called when the plugin is being unloaded."""
        logger.info("Saving Revolver Game plugin data...")
        self._save_runtime_data()
        logger.info("Revolver Game plugin data saved.")

    # --- Command Handlers & Game Logic ---

    @filter.command("走火开", "misfire on")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_misfire_on(self, event: AstrMessageEvent):
        """开启本群的走火功能"""
        if not event.is_admin:
            return Comp.text("抱歉，只有群管理员才能操作哦。")
        group_id = str(event.message.group_id)
        self.group_misfire_switches[group_id] = True
        self._save_runtime_data()
        return Comp.text("本群左轮手枪走火功能已开启！")

    @filter.command("走火关", "misfire off")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_misfire_off(self, event: AstrMessageEvent):
        """关闭本群的走火功能"""
        if not event.is_admin:
            return Comp.text("抱歉，只有群管理员才能操作哦。")
        group_id = str(event.message.group_id)
        self.group_misfire_switches[group_id] = False
        self._save_runtime_data()
        return Comp.text("本群左轮手枪走火功能已关闭！")

    @filter.command("装填", "load")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_load(self, event: AstrMessageEvent, bullets: int = 1):
        """装填子弹开始一轮游戏。"""
        group_id = str(event.message.group_id)
        sender_nickname = event.message.sender.nickname
        if group_id in self.group_states:
            return Comp.text(f"{sender_nickname}，游戏还未结束，不能重新装填！")
        if not 1 <= bullets <= 6:
            return Comp.text(f"{sender_nickname}，装填的实弹数量必须在 1 到 6 之间。")
        chambers = [False] * 6
        positions = random.sample(range(6), bullets)
        for pos in positions:
            chambers[pos] = True
        self.group_states[group_id] = {'chambers': chambers, 'current_chamber_index': 0}
        self._start_timeout_timer(group_id, 180)
        return Comp.text(f"{sender_nickname} 装填了 {bullets} 发实弹，输入 /开枪 开始游戏！")

    @filter.command("开枪", "shoot")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_shoot(self, event: AstrMessageEvent):
        """开枪！"""
        group_id = str(event.message.group_id)
        sender_nickname = event.message.sender.nickname
        group_state = self.group_states.get(group_id)
        if not group_state:
            return Comp.text(f"{sender_nickname}，枪里没有子弹，请先装填。")
        self._start_timeout_timer(group_id, 180)
        chambers = group_state['chambers']
        current_index = group_state['current_chamber_index']
        if chambers[current_index]:
            await self._handle_hit(event, group_state)
        else:
            await self._handle_miss(event, group_state)
        if sum(group_state['chambers']) == 0:
            self._clear_game_state(group_id)
            await event.reply(Comp.text("所有实弹都已射出，游戏结束。"))

    # --- Passive Misfire Listener ---

    @filter.on_message(priority=100)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """Listens to all group messages for a chance of a passive misfire."""
        if event.is_self:
            return
        group_id = str(event.message.group_id)
        if self.group_misfire_switches.get(group_id, False):
            if random.random() <= self.misfire_probability:
                await self._trigger_misfire(event)

    # --- Helper & Logic Methods ---

    async def _ban_user(self, event: AstrMessageEvent) -> Comp.Text | None:
        """Bans the user for a random duration."""
        try:
            duration = random.randint(self.min_ban_duration, self.max_ban_duration)
            await event.bot.ban_group_member(
                group_id=event.message.group_id,
                user_id=event.message.sender.user_id,
                duration=duration
            )
        except Exception as e:
            logger.error(f"Failed to ban user {event.message.sender.user_id}: {e}")
            return Comp.text(f"Oops, 我没有权限禁言 {event.message.sender.nickname}！")
        return None

    async def _trigger_misfire(self, event: AstrMessageEvent):
        """Handles the misfire event."""
        sender_nickname = event.message.sender.nickname
        misfire_desc = random.choice(self.texts.get('misfire_descriptions', []))
        user_reaction = random.choice(self.texts.get('user_reactions', [])).format(sender_nickname=sender_nickname)
        message = f"{misfire_desc} {user_reaction} 不幸被击中！"
        await event.reply(Comp.text(message))
        ban_result = await self._ban_user(event)
        if ban_result:
            await event.reply(ban_result)

    async def _handle_hit(self, event: AstrMessageEvent, group_state: dict):
        """Handles a successful shot."""
        sender_nickname = event.message.sender.nickname
        group_state['chambers'][group_state['current_chamber_index']] = False
        group_state['current_chamber_index'] = (group_state['current_chamber_index'] + 1) % 6
        trigger_desc = random.choice(self.texts.get('trigger_descriptions', []))
        user_reaction = random.choice(self.texts.get('user_reactions', [])).format(sender_nickname=sender_nickname)
        message = f"{trigger_desc}，{user_reaction}"
        await event.reply(Comp.text(message))
        ban_result = await self._ban_user(event)
        if ban_result:
            await event.reply(ban_result)

    async def _handle_miss(self, event: AstrMessageEvent, group_state: dict):
        """Handles a miss."""
        sender_nickname = event.message.sender.nickname
        group_state['current_chamber_index'] = (group_state['current_chamber_index'] + 1) % 6
        miss_message = random.choice(self.texts.get('miss_messages', [])).format(sender_nickname=sender_nickname)
        await event.reply(Comp.text(miss_message))

    def _start_timeout_timer(self, group_id: str, seconds: int):
        """Starts or resets the game's inactivity timeout timer."""
        job_id = f"rg_timeout_{group_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        run_time = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
        self.scheduler.add_job(
            self._clear_game_state, 'date', run_date=run_time,
            args=[group_id, "(Timeout)"], id=job_id
        )

    def _clear_game_state(self, group_id: str, reason: str = ""):
        """Clears the game state and timer for a specific group."""
        job_id = f"rg_timeout_{group_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        if group_id in self.group_states:
            del self.group_states[group_id]
            logger.info(f"Revolver game state for group {group_id} has been cleared {reason}.")

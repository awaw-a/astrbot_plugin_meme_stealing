from __future__ import annotations

import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .config import PLUGIN_NAME, MemeStealingConfig, get_plugin_data_dir, normalize_str_list
    from .database import MemeDatabase, MemeRecord
    from .image_store import (
        ImageCandidate,
        ImageStore,
        extract_images_from_event,
        extract_reply_images_from_event,
    )
    from .llm import MemeLLMTagger
    from .matcher import KeywordMatcher
    from .panel.server import PanelServer
except ImportError:
    from config import PLUGIN_NAME, MemeStealingConfig, get_plugin_data_dir, normalize_str_list
    from database import MemeDatabase, MemeRecord
    from image_store import (
        ImageCandidate,
        ImageStore,
        extract_images_from_event,
        extract_reply_images_from_event,
    )
    from llm import MemeLLMTagger
    from matcher import KeywordMatcher
    from panel.server import PanelServer


@dataclass
class RecentImage:
    candidate: ImageCandidate
    group_id: str
    message_id: str
    created_at: float


@register(
    PLUGIN_NAME,
    "Codex",
    "QQ 群表情包采集、LLM 标注、关键词自动回复和本地管理面板",
    "0.1.0",
)
class MemeStealingPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = MemeStealingConfig.from_mapping(config)
        self.data_dir = get_plugin_data_dir(PLUGIN_NAME)
        self.images_dir = self.data_dir / "images"
        self.db = MemeDatabase(self.data_dir / "memes.sqlite3")
        self.image_store = ImageStore(self.images_dir, self.config.max_image_bytes)
        self.tagger = MemeLLMTagger(context, self.config)
        self.matcher = KeywordMatcher(self.config.match_threshold)
        self.panel: PanelServer | None = None
        self._terminated = False

        self.recent_images: dict[str, deque[RecentImage]] = defaultdict(
            lambda: deque(maxlen=self.config.recent_image_cache_size)
        )
        self.collect_cooldown: dict[str, float] = {}
        self.reply_cooldown: dict[str, float] = {}

    async def initialize(self):
        self._terminated = False
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        if self.config.panel_enabled:
            try:
                self.panel = PanelServer(
                    self.db,
                    self.config.panel_host,
                    self.config.panel_port,
                    self.config.admin_token,
                )
                self.panel.start()
                logger.info(f"meme stealing: 管理面板已启动 {self.panel.url}")
            except Exception as exc:
                logger.warning(f"meme stealing: 管理面板启动失败: {exc}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """统一处理群消息和指令。

        说明：这里没有使用 @filter.command，是为了让 /meme_desc <id> <带空格描述>
        这类命令不受不同 AstrBot 版本参数解析差异影响。
        """
        if self._terminated:
            return

        text = (getattr(event, "message_str", "") or "").strip()
        if not self.config.enabled:
            if self._is_meme_command(text):
                result = event.plain_result("插件当前已在配置中禁用，无法执行该指令。")
                if not await self._send_command_result(event, result):
                    yield result
                self._stop_event(event)
            return
        if self._is_from_self(event):
            return

        if self._is_meme_command(text):
            result_text = await self._safe_handle_command(event, text)
            result = event.plain_result(result_text)
            if not await self._send_command_result(event, result):
                yield result
            self._stop_event(event)
            return

        group_id = self._get_group_id(event)
        if not group_id or not self.config.group_allowed(group_id):
            return

        candidates = extract_images_from_event(event)
        if candidates:
            self._remember_images(event, group_id, candidates)
            if self._should_auto_collect(group_id):
                await self._save_candidate(event, candidates[0], group_id=group_id, force=True)
                self.collect_cooldown[group_id] = time.time()

        if not text or self._looks_like_command(text):
            return

        if not self._group_auto_reply_enabled(group_id):
            return
        if random.random() > self.config.auto_reply_probability:
            return
        if not self._cooldown_ready(self.reply_cooldown, group_id, self.config.auto_reply_cooldown_seconds):
            return

        match = self.matcher.choose(text, self.db.list_enabled())
        if not match:
            return

        self.reply_cooldown[group_id] = time.time()
        self.db.increment_use_count(match.record.id)
        yield event.image_result(str(Path(match.record.file_path)))

    async def terminate(self):
        self._terminated = True
        if self.panel:
            await self.panel.stop()
        self.db.close()

    async def _handle_command(self, event: AstrMessageEvent, text: str) -> str:
        command, args = split_command(text)
        group_id = self._get_group_id(event)

        if command in {"meme_save", "保存表情"}:
            return await self._cmd_save(event, group_id, args)
        if command == "meme_on":
            if not self._ensure_group(group_id):
                return "这个指令需要在群聊中使用。"
            if not self._is_admin(event):
                return "没有权限。"
            self.db.set_group_auto_reply(group_id, True)
            return "已开启当前群的自动表情回复。"
        if command == "meme_off":
            if not self._ensure_group(group_id):
                return "这个指令需要在群聊中使用。"
            if not self._is_admin(event):
                return "没有权限。"
            self.db.set_group_auto_reply(group_id, False)
            return "已关闭当前群的自动表情回复。"
        if command == "meme_list":
            if not self._is_admin(event):
                return "没有权限。"
            return self._cmd_list(args)
        if command == "meme_delete":
            if not self._is_admin(event):
                return "没有权限。"
            return self._cmd_delete(args)
        if command == "meme_desc":
            if not self._is_admin(event):
                return "没有权限。"
            return self._cmd_desc(args)
        if command == "meme_tags":
            if not self._is_admin(event):
                return "没有权限。"
            return self._cmd_tags(args)
        if command == "meme_panel":
            if not self._is_admin(event):
                return "没有权限。"
            if not self.config.panel_enabled:
                return "管理面板未启用，请在插件配置中开启 panel_enabled。"
            return (
                f"管理面板地址：{self.config.panel_url}\n"
                f"{self.config.panel_access_hint}\n"
                "允许公网访问时请务必使用强 token，并配合防火墙或反向代理限制访问来源。"
            )
        if command == "meme_stats":
            if not self._is_admin(event):
                return "没有权限。"
            return self._cmd_stats()
        return ""

    async def _safe_handle_command(self, event: AstrMessageEvent, text: str) -> str:
        command, _ = split_command(text)
        try:
            result = await self._handle_command(event, text)
        except Exception as exc:
            logger.error(f"meme stealing: 指令 {text} 执行失败: {exc}")
            return f"指令执行失败：{exc}\n{command_usage(command)}"
        if not result:
            return f"指令没有产生结果，请检查用法。\n{command_usage(command)}"
        return result

    async def _send_command_result(self, event: AstrMessageEvent, result: Any) -> bool:
        """优先主动发送指令反馈，避免 stop_event/管线阶段差异吞掉 yield 结果。"""
        send = getattr(event, "send", None)
        if not callable(send):
            return False
        try:
            await send(result)
            return True
        except Exception as exc:
            logger.warning(f"meme stealing: 主动发送指令反馈失败，将回退到 yield: {exc}")
            return False

    async def _cmd_save(self, event: AstrMessageEvent, group_id: str, args: str) -> str:
        if not self._ensure_group(group_id):
            return "这个指令需要在群聊中使用。"

        candidates: list[ImageCandidate] = []
        if args.strip().lower() == "latest":
            latest = self._get_latest_image(group_id)
            if latest:
                candidates = [latest.candidate]
        else:
            candidates = extract_reply_images_from_event(event)
            if not candidates:
                candidates = extract_images_from_event(event)

        if not candidates:
            return "没有找到可保存的图片。请回复一条图片消息后发送 /meme_save，或使用 /meme_save latest 保存最近一张图。"

        record, duplicate, error = await self._save_candidate(
            event,
            candidates[0],
            group_id=group_id,
            force=True,
        )
        if error:
            return f"保存失败：{error}"
        if not record:
            return "保存失败：没有生成数据库记录。"

        prefix = "已存在" if duplicate else "保存成功"
        return f"{prefix}：#{record.id}\n描述：{record.description or '待审核'}\n标签：{', '.join(record.tags) or '待审核'}"

    def _cmd_list(self, args: str) -> str:
        try:
            limit = min(max(int(args.strip() or "10"), 1), 30)
        except ValueError:
            limit = 10
        records = self.db.list_memes(limit=limit)
        if not records:
            return "还没有保存表情包。"
        lines = ["最近保存的表情包："]
        for record in records:
            tags = ",".join(record.tags[:5]) or "无标签"
            status = "待审核" if record.pending_review else ("启用" if record.enabled else "禁用")
            lines.append(f"#{record.id} [{status}] {record.description or '无描述'} | {tags}")
        return "\n".join(lines)

    def _cmd_delete(self, args: str) -> str:
        meme_id = parse_int_arg(args)
        if not meme_id:
            return "用法：/meme_delete <id>"
        if not self.db.delete_meme(meme_id):
            return f"未找到 #{meme_id}。"
        return f"已删除 #{meme_id}。"

    def _cmd_desc(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            return "用法：/meme_desc <id> <新描述>"
        record = self.db.update_meme(int(parts[0]), description=parts[1].strip(), pending_review=False)
        if not record:
            return f"未找到 #{parts[0]}。"
        return f"已更新 #{record.id} 描述。"

    def _cmd_tags(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            return "用法：/meme_tags <id> <tag1,tag2,tag3>"
        tags = normalize_str_list(parts[1])
        record = self.db.update_meme(int(parts[0]), tags=tags, pending_review=False)
        if not record:
            return f"未找到 #{parts[0]}。"
        return f"已更新 #{record.id} 标签：{', '.join(record.tags) or '无'}"

    def _cmd_stats(self) -> str:
        stats = self.db.stats()
        return (
            "表情包统计：\n"
            f"总数：{stats['total']}\n"
            f"启用：{stats['enabled']}\n"
            f"待审核：{stats['pending_review']}\n"
            f"今日保存：{stats['saved_today']}\n"
            f"累计发送：{stats['use_count']}"
        )

    async def _save_candidate(
        self,
        event: AstrMessageEvent,
        candidate: ImageCandidate,
        *,
        group_id: str,
        force: bool,
    ) -> tuple[MemeRecord | None, bool, str]:
        try:
            stored = await self.image_store.save_candidate(candidate)
        except Exception as exc:
            logger.warning(f"meme stealing: 图片保存失败: {exc}")
            return None, False, str(exc)

        existing = self.db.find_by_hash(stored.hash)
        if existing:
            return existing, True, ""

        description, tags, emotion, pending_review = await self.tagger.describe_image(
            stored.file_path,
            umo=getattr(event, "unified_msg_origin", None),
        )
        if not description and not pending_review:
            description = "未生成描述"
        source_user_id = self._get_sender_id(event) if self.config.store_sender_id else None
        record = self.db.create_meme(
            file_path=str(stored.file_path),
            hash_value=stored.hash,
            description=description,
            tags=tags,
            emotion=emotion,
            source_group_id=group_id,
            source_user_id=source_user_id,
            pending_review=pending_review,
            enabled=True,
        )
        return record, False, ""

    def _remember_images(
        self,
        event: AstrMessageEvent,
        group_id: str,
        candidates: list[ImageCandidate],
    ) -> None:
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        for candidate in candidates:
            candidate.message_id = message_id
            self.recent_images[group_id].append(
                RecentImage(
                    candidate=candidate,
                    group_id=group_id,
                    message_id=message_id,
                    created_at=time.time(),
                )
            )

    def _get_latest_image(self, group_id: str) -> RecentImage | None:
        images = self.recent_images.get(group_id)
        if not images:
            return None
        return images[-1]

    def _should_auto_collect(self, group_id: str) -> bool:
        if not self.config.auto_collect_enabled:
            return False
        if not self._cooldown_ready(
            self.collect_cooldown,
            group_id,
            self.config.auto_collect_cooldown_seconds,
        ):
            return False
        if self.config.max_images_per_day and self.db.count_saved_today() >= self.config.max_images_per_day:
            return False
        return random.random() < self.config.collect_probability

    def _group_auto_reply_enabled(self, group_id: str) -> bool:
        stored = self.db.get_group_auto_reply(group_id)
        if stored is not None:
            return stored
        return self.config.auto_reply_enabled

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        if not self.config.admin_users:
            return True
        return self._get_sender_id(event) in self.config.admin_users

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = getattr(getattr(event, "message_obj", None), "group_id", "")
        return str(group_id or "")

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        try:
            sender_id = event.get_sender_id()
        except Exception:
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            sender_id = (
                getattr(sender, "user_id", None)
                or getattr(sender, "id", None)
                or getattr(sender, "sender_id", None)
                or ""
            )
        return str(sender_id or "")

    def _is_from_self(self, event: AstrMessageEvent) -> bool:
        message_obj = getattr(event, "message_obj", None)
        self_id = str(getattr(message_obj, "self_id", "") or "")
        sender_id = self._get_sender_id(event)
        return bool(self_id and sender_id and self_id == sender_id)

    @staticmethod
    def _cooldown_ready(bucket: dict[str, float], key: str, seconds: int) -> bool:
        if seconds <= 0:
            return True
        return time.time() - bucket.get(key, 0.0) >= seconds

    @staticmethod
    def _ensure_group(group_id: str) -> bool:
        return bool(group_id)

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        return text.startswith(("/", "／"))

    @staticmethod
    def _is_meme_command(text: str) -> bool:
        if not text.strip():
            return False
        command, _ = split_command(text)
        return command in {
            "meme_on",
            "meme_off",
            "meme_save",
            "保存表情",
            "meme_list",
            "meme_delete",
            "meme_desc",
            "meme_tags",
            "meme_panel",
            "meme_stats",
        }

    @staticmethod
    def _stop_event(event: AstrMessageEvent) -> None:
        try:
            event.stop_event()
        except Exception:
            pass


def split_command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if stripped.startswith(("／", "/")):
        stripped = stripped[1:]
    if not stripped:
        return "", ""
    parts = stripped.split(maxsplit=1)
    command = parts[0].strip().lower()
    args = parts[1] if len(parts) > 1 else ""
    return command, args


def command_usage(command: str) -> str:
    usages = {
        "meme_on": "用法：/meme_on，开启当前群自动表情回复。",
        "meme_off": "用法：/meme_off，关闭当前群自动表情回复。",
        "meme_save": "用法：回复图片后发送 /meme_save，或发送 /meme_save latest 保存最近一张图。",
        "保存表情": "用法：回复图片后发送 /保存表情，或发送 /meme_save latest 保存最近一张图。",
        "meme_list": "用法：/meme_list [数量]，列出最近保存的表情包。",
        "meme_delete": "用法：/meme_delete <id>，删除指定表情包。",
        "meme_desc": "用法：/meme_desc <id> <新描述>，修改描述。",
        "meme_tags": "用法：/meme_tags <id> <tag1,tag2,tag3>，修改标签。",
        "meme_panel": "用法：/meme_panel，获取管理面板地址。",
        "meme_stats": "用法：/meme_stats，查看表情包统计。",
    }
    return usages.get(command, "可用指令：/meme_on、/meme_off、/meme_save latest、/meme_list、/meme_stats、/meme_panel")


def parse_int_arg(args: str) -> int | None:
    first = (args.strip().split(maxsplit=1) or [""])[0]
    if not first.isdigit():
        return None
    return int(first)

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 本地单测时没有 AstrBot 包。
    import logging

    logger = logging.getLogger(__name__)

try:
    from .config import MemeStealingConfig
except ImportError:
    from config import MemeStealingConfig


class MemeLLMTagger:
    def __init__(self, context: Any, config: MemeStealingConfig):
        self.context = context
        self.config = config
        self._lock = asyncio.Lock()
        self._last_call_at = 0.0

    async def describe_image(
        self,
        image_path: Path,
        *,
        umo: str | None = None,
    ) -> tuple[str, list[str], list[str], bool]:
        """调用 AstrBot 已配置多模态模型生成描述。

        返回值最后一项表示 pending_review。这里使用 Provider.text_chat 的 image_urls
        参数；若本地 AstrBot 版本该接口签名有变化，需要按当前版本调整这一处。
        """
        provider = self._get_provider(umo)
        if provider is None:
            return "", [], [], self.config.pending_review_when_llm_failed

        async with self._lock:
            wait_seconds = self.config.llm_min_interval_seconds - (time.time() - self._last_call_at)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            self._last_call_at = time.time()

            try:
                resp = await provider.text_chat(
                    prompt=build_prompt(),
                    system_prompt="你是表情包归档助手，只输出合法 JSON，不要输出 Markdown。",
                    image_urls=[str(image_path.resolve())],
                )
            except Exception as exc:
                logger.warning(f"meme stealing: LLM 标注失败: {exc}")
                return "", [], [], self.config.pending_review_when_llm_failed

        text = getattr(resp, "completion_text", "") or str(resp)
        data = parse_json_object(text)
        if not data:
            logger.warning(f"meme stealing: LLM 返回不是 JSON: {text[:200]}")
            return "", [], [], self.config.pending_review_when_llm_failed

        description = str(data.get("description", "")).strip()
        tags = normalize_list(data.get("tags"))
        emotion = normalize_list(data.get("emotion"))
        pending = not description and self.config.pending_review_when_llm_failed
        return description, tags, emotion, pending

    def _get_provider(self, umo: str | None):
        provider_id = (self.config.llm_provider or "").strip()
        try:
            if provider_id and hasattr(self.context, "get_provider_by_id"):
                return self.context.get_provider_by_id(provider_id=provider_id)
            if hasattr(self.context, "get_using_provider"):
                try:
                    return self.context.get_using_provider(umo=umo)
                except TypeError:
                    return self.context.get_using_provider()
        except Exception as exc:
            logger.warning(f"meme stealing: 获取 LLM provider 失败: {exc}")
        return None


def build_prompt() -> str:
    return (
        "请分析这张群聊表情包/图片，生成适合自动匹配聊天场景的简短元数据。\n"
        "要求：\n"
        "1. description 用一句中文描述图片内容和适用场景，30 字以内。\n"
        "2. tags 给出 3 到 8 个中文关键词，包含主体、梗点、动作或语境。\n"
        "3. emotion 给出 1 到 4 个中文情绪/场景词。\n"
        "4. 只输出 JSON 对象，不要解释，不要代码块。\n"
        '示例：{"description":"一只猫露出疑惑表情，适合表达不理解或震惊","tags":["疑惑","震惊","猫","不理解"],"emotion":["困惑","惊讶"]}'
    )


def parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)

    candidates = [stripped]
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[,，、\s]+", value)
    elif isinstance(value, list):
        items = value
    else:
        return []
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result[:12]

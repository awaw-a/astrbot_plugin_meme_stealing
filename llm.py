from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
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


@dataclass
class ImageAnalysis:
    description: str = ""
    tags: list[str] | None = None
    emotion: list[str] | None = None
    pending_review: bool = False
    is_meme: bool | None = None
    meme_confidence: float = 0.0
    meme_reason: str = ""
    check_failed: bool = False

    def __post_init__(self) -> None:
        self.tags = self.tags or []
        self.emotion = self.emotion or []


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
        analysis = await self.analyze_image(image_path, umo=umo)
        return analysis.description, analysis.tags or [], analysis.emotion or [], analysis.pending_review

    async def analyze_image(
        self,
        image_path: Path,
        *,
        umo: str | None = None,
    ) -> ImageAnalysis:
        """调用 AstrBot 已配置多模态模型生成描述。

        这里使用 Provider.text_chat 的 image_urls
        参数；若本地 AstrBot 版本该接口签名有变化，需要按当前版本调整这一处。
        """
        provider = self._get_provider(umo)
        if provider is None:
            return ImageAnalysis(
                pending_review=self.config.pending_review_when_llm_failed,
                check_failed=True,
                meme_reason="未找到可用的多模态 LLM provider",
            )

        async with self._lock:
            wait_seconds = self.config.llm_min_interval_seconds - (time.time() - self._last_call_at)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            self._last_call_at = time.time()

            try:
                resp = await provider.text_chat(
                    prompt=build_prompt(),
                    system_prompt="你是表情包归档与筛选助手，只输出合法 JSON，不要输出 Markdown。",
                    image_urls=[str(image_path.resolve())],
                )
            except Exception as exc:
                logger.warning(f"meme stealing: LLM 标注失败: {exc}")
                return ImageAnalysis(
                    pending_review=self.config.pending_review_when_llm_failed,
                    check_failed=True,
                    meme_reason=f"LLM 调用失败: {exc}",
                )

        text = getattr(resp, "completion_text", "") or str(resp)
        data = parse_json_object(text)
        if not data:
            logger.warning(f"meme stealing: LLM 返回不是 JSON: {text[:200]}")
            return ImageAnalysis(
                pending_review=self.config.pending_review_when_llm_failed,
                check_failed=True,
                meme_reason="LLM 返回不是 JSON",
            )

        description = str(data.get("description", "")).strip()
        tags = normalize_list(data.get("tags"))
        emotion = normalize_list(data.get("emotion"))
        pending = not description and self.config.pending_review_when_llm_failed
        return ImageAnalysis(
            description=description,
            tags=tags,
            emotion=emotion,
            pending_review=pending,
            is_meme=parse_optional_bool(data.get("is_meme")),
            meme_confidence=clamp_float(data.get("meme_confidence"), 0.0, 1.0),
            meme_reason=str(data.get("meme_reason", "")).strip(),
            check_failed=False,
        )

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
        "请分析这张群聊图片，先判断它是否应该作为“表情包/梗图/反应图/贴纸”保存，再生成适合自动匹配聊天场景的简短元数据。\n"
        "判定为表情包的标准：图片主要用于表达情绪、态度、吐槽、玩梗、幽默回应，包含常见表情包、贴纸、自定义表情、反应图、配字梗图、适合聊天回复的搞笑截图。\n"
        "判定为非表情包的标准：普通风景/自拍/生活照、文档/票据/二维码/广告、聊天记录截图、无明确情绪表达的普通图片、仅用于传递信息而非聊天回应的图片。\n"
        "要求：\n"
        "1. is_meme 输出 true 或 false。\n"
        "2. meme_confidence 输出 0 到 1 的数字，表示你对 is_meme 判定的置信度。\n"
        "3. meme_reason 用一句中文说明判定原因，20 字以内。\n"
        "4. description 用一句中文描述图片内容和适用场景，30 字以内；非表情包也要简短描述。\n"
        "5. tags 给出 3 到 8 个中文关键词，包含主体、梗点、动作或语境。\n"
        "6. emotion 给出 1 到 4 个中文情绪/场景词，非表情包可为空数组。\n"
        "7. 只输出 JSON 对象，不要解释，不要代码块。\n"
        '示例：{"is_meme":true,"meme_confidence":0.92,"meme_reason":"猫表情可用于疑惑回应","description":"一只猫露出疑惑表情，适合表达不理解或震惊","tags":["疑惑","震惊","猫","不理解"],"emotion":["困惑","惊讶"]}'
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


def parse_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1", "是", "表情包"}:
            return True
        if text in {"false", "no", "0", "否", "不是"}:
            return False
    return None


def clamp_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return min(max(number, minimum), maximum)

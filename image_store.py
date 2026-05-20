from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ImageCandidate:
    source: Any
    source_type: str
    component: Any | None = None
    message_id: str = ""


@dataclass
class StoredImage:
    file_path: Path
    hash: str
    size: int
    duplicate: bool = False


class ImageStore:
    def __init__(self, image_dir: Path, max_bytes: int):
        self.image_dir = Path(image_dir)
        self.max_bytes = max_bytes
        self.image_dir.mkdir(parents=True, exist_ok=True)

    async def save_candidate(self, candidate: ImageCandidate) -> StoredImage:
        data, suffix = await load_candidate_image_bytes(candidate, self.max_bytes)
        if not data:
            raise ValueError("图片内容为空")
        if len(data) > self.max_bytes:
            raise ValueError("图片超过大小限制")

        suffix = suffix or detect_image_suffix(data) or ".jpg"
        image_hash = hashlib.sha256(data).hexdigest()
        target = self.image_dir / f"{image_hash[:24]}{suffix}"
        duplicate = target.exists()
        if not duplicate:
            target.write_bytes(data)
        return StoredImage(file_path=target, hash=image_hash, size=len(data), duplicate=duplicate)


def extract_images_from_event(event: Any) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    try:
        chain = event.get_messages()
    except Exception:
        chain = getattr(getattr(event, "message_obj", None), "message", []) or []
    candidates.extend(extract_images_from_chain(chain))

    # 有些适配器会把更完整的消息段放在 raw_message 里。
    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    candidates.extend(extract_images_from_raw(raw_message))
    return dedupe_candidates(candidates)


def extract_reply_images_from_event(event: Any) -> list[ImageCandidate]:
    """尽力从回复/引用消息里找图片。

    回复消息在不同 QQ 适配器上的字段差异较大：可能是 Reply/Quote 组件，也可能
    藏在 raw_message.reply、raw_message.quote 或原始 message 段里。这里做保守递归，
    如果本地 AstrBot 版本有专门的 get_reply_message API，可在 main.py 调用前替换。
    """
    candidates: list[ImageCandidate] = []
    try:
        chain = event.get_messages()
    except Exception:
        chain = getattr(getattr(event, "message_obj", None), "message", []) or []

    for component in chain or []:
        name = component.__class__.__name__.lower()
        if name in {"reply", "quote", "replymessage"}:
            candidates.extend(extract_images_from_any(component))
            for attr in ("message", "messages", "chain", "content", "raw_message"):
                candidates.extend(extract_images_from_any(getattr(component, attr, None)))

    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw_message is not None:
        candidates.extend(extract_images_from_reply_raw(raw_message))
    return dedupe_candidates(candidates)


def extract_images_from_any(value: Any) -> list[ImageCandidate]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return extract_images_from_chain(value)
    if isinstance(value, dict):
        return extract_images_from_raw(value)
    return extract_images_from_chain([value])


def extract_images_from_chain(chain: Any) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    if not isinstance(chain, (list, tuple)):
        return candidates
    for component in chain:
        candidates.extend(extract_image_from_component(component))
    return candidates


def extract_image_from_component(component: Any) -> list[ImageCandidate]:
    if component is None:
        return []
    if isinstance(component, dict):
        return extract_images_from_raw(component)

    name = component.__class__.__name__.lower()
    if name != "image" and not hasattr(component, "url") and not hasattr(component, "file"):
        return []

    values: list[tuple[Any, str]] = []
    for attr in ("url", "path", "file_path", "file", "data", "base64"):
        value = getattr(component, attr, None)
        if callable(value):
            continue
        if value:
            values.append((value, attr))

    candidates = [
        ImageCandidate(source=value, source_type=guess_source_type(value, hint), component=component)
        for value, hint in values
    ]
    if not candidates and has_component_converter(component):
        candidates.append(ImageCandidate(source=component, source_type="component", component=component))
    return candidates


def extract_images_from_raw(raw: Any) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    if raw is None:
        return candidates

    if isinstance(raw, dict):
        raw_type = str(raw.get("type") or raw.get("post_type") or "").lower()
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        if raw_type == "image" or raw.get("type") == "image":
            for key in ("url", "path", "file_path", "file", "data", "base64"):
                value = data.get(key) if isinstance(data, dict) else None
                if value:
                    candidates.append(ImageCandidate(value, guess_source_type(value, key)))

        for key in ("message", "raw_message", "reply", "quote", "source", "content"):
            if key in raw:
                candidates.extend(extract_images_from_raw(raw[key]))
        return candidates

    if isinstance(raw, (list, tuple)):
        for item in raw:
            candidates.extend(extract_images_from_raw(item))
    return candidates


def extract_images_from_reply_raw(raw: Any) -> list[ImageCandidate]:
    if not isinstance(raw, dict):
        return extract_images_from_raw(raw)
    candidates: list[ImageCandidate] = []
    for key in ("reply", "quote", "source", "message"):
        if key in raw:
            value = raw[key]
            if key == "message":
                # OneBot 当前消息的 message 里可能只有 reply 段；递归后会过滤出图片。
                candidates.extend(extract_images_from_raw(value))
            else:
                candidates.extend(extract_images_from_raw(value))
    return candidates


def dedupe_candidates(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    seen: set[str] = set()
    result: list[ImageCandidate] = []
    for item in candidates:
        key = str(item.source)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def guess_source_type(value: Any, hint: str = "") -> str:
    if isinstance(value, bytes):
        return "bytes"
    text = str(value)
    if text.startswith(("http://", "https://")):
        return "url"
    if text.startswith("file://"):
        return "file"
    if text.startswith(("base64://", "data:image/")) or hint == "base64":
        return "base64"
    return "file"


async def load_candidate_image_bytes(candidate: ImageCandidate, max_bytes: int) -> tuple[bytes, str]:
    if candidate.source_type == "component" or candidate.component is not None:
        try:
            return await load_component_image_bytes(candidate.component or candidate.source, max_bytes)
        except ValueError:
            if candidate.source_type == "component":
                raise

    try:
        return await load_image_bytes(candidate.source, max_bytes)
    except ValueError as exc:
        if candidate.component is not None:
            try:
                return await load_component_image_bytes(candidate.component, max_bytes)
            except ValueError:
                pass
        raise ValueError(f"{exc}；来源字段={candidate.source_type}:{short_source(candidate.source)}") from exc


async def load_component_image_bytes(component: Any, max_bytes: int) -> tuple[bytes, str]:
    if component is None:
        raise ValueError("图片组件为空")

    convert_to_file_path = getattr(component, "convert_to_file_path", None)
    if callable(convert_to_file_path):
        path = await maybe_await(convert_to_file_path())
        if path:
            return await load_image_bytes(path, max_bytes)

    convert_to_base64 = getattr(component, "convert_to_base64", None)
    if callable(convert_to_base64):
        raw_base64 = await maybe_await(convert_to_base64())
        if raw_base64:
            payload = str(raw_base64)
            if not payload.startswith(("base64://", "data:image/")):
                payload = f"base64://{payload}"
            data, suffix = decode_base64_image(payload)
            if len(data) > max_bytes:
                raise ValueError("图片超过大小限制")
            return data, suffix

    raise ValueError("图片组件没有可用的 convert_to_file_path/convert_to_base64 方法")


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def load_image_bytes(source: Any, max_bytes: int) -> tuple[bytes, str]:
    if isinstance(source, bytes):
        return source, detect_image_suffix(source)

    text = str(source).strip()
    if text.startswith(("http://", "https://")):
        return await download_url(text, max_bytes)
    if text.startswith("file://"):
        parsed = urllib.parse.urlparse(text)
        return await read_local_file(Path(urllib.request.url2pathname(parsed.path)), max_bytes)
    if text.startswith(("base64://", "data:image/")):
        return decode_base64_image(text)

    path = Path(text)
    if path.exists():
        return await read_local_file(path, max_bytes)

    raise ValueError("无法识别图片来源，可能需要按本地适配器调整 Image 组件字段")


async def read_local_file(path: Path, max_bytes: int) -> tuple[bytes, str]:
    if path.stat().st_size > max_bytes:
        raise ValueError("图片超过大小限制")

    def _read() -> bytes:
        return path.read_bytes()

    data = await asyncio.to_thread(_read)
    return data, path.suffix.lower() or detect_image_suffix(data)


async def download_url(url: str, max_bytes: int) -> tuple[bytes, str]:
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                length = resp.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError("图片超过大小限制")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("图片超过大小限制")
                    chunks.append(chunk)
                data = b"".join(chunks)
                return data, suffix_from_content_type(resp.headers.get("Content-Type")) or detect_image_suffix(data)
    except ImportError:
        return await asyncio.to_thread(download_url_stdlib, url, max_bytes)


def download_url_stdlib(url: str, max_bytes: int) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "AstrBot-Meme-Stealing/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ValueError("图片超过大小限制")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("图片超过大小限制")
            return data, suffix_from_content_type(response.headers.get("Content-Type")) or detect_image_suffix(data)
    except urllib.error.URLError as exc:
        raise ValueError(f"图片下载失败: {exc}") from exc


def decode_base64_image(text: str) -> tuple[bytes, str]:
    suffix = ".jpg"
    payload = text
    if text.startswith("data:image/"):
        header, payload = text.split(",", 1)
        media_type = header.split(";", 1)[0].replace("data:", "")
        suffix = suffix_from_content_type(media_type) or suffix
    elif text.startswith("base64://"):
        payload = text[len("base64://") :]
    data = base64.b64decode(payload)
    return data, detect_image_suffix(data) or suffix


def detect_image_suffix(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".jpg"


def suffix_from_content_type(content_type: str | None) -> str:
    if not content_type:
        return ""
    media_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(media_type, "")


def has_component_converter(component: Any) -> bool:
    return callable(getattr(component, "convert_to_file_path", None)) or callable(
        getattr(component, "convert_to_base64", None)
    )


def short_source(source: Any) -> str:
    text = str(source)
    text = text.replace("\n", "\\n")
    if len(text) > 120:
        return text[:117] + "..."
    return text

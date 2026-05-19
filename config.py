from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PLUGIN_NAME = "astrbot_plugin_meme_stealing"


@dataclass
class MemeStealingConfig:
    enabled: bool = True
    auto_collect_enabled: bool = True
    auto_reply_enabled: bool = False
    collect_probability: float = 0.05
    auto_reply_probability: float = 1.0
    max_images_per_day: int = 50
    image_max_size_mb: float = 8.0
    recent_image_cache_size: int = 30
    group_whitelist: list[str] = field(default_factory=list)
    group_blacklist: list[str] = field(default_factory=list)
    auto_reply_cooldown_seconds: int = 120
    auto_collect_cooldown_seconds: int = 60
    admin_users: list[str] = field(default_factory=list)
    admin_token: str = "change-me"
    panel_enabled: bool = True
    panel_host: str = "127.0.0.1"
    panel_port: int = 8756
    llm_provider: str = ""
    llm_min_interval_seconds: float = 6.0
    pending_review_when_llm_failed: bool = True
    match_threshold: float = 1.0
    store_sender_id: bool = False

    @classmethod
    def from_mapping(cls, raw: Any) -> "MemeStealingConfig":
        """从 AstrBotConfig/dict 构造配置；缺失项全部使用安全默认值。"""
        if raw is None:
            return cls()

        data = dict(raw)
        defaults = cls()
        values: dict[str, Any] = {}
        for field_name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            values[field_name] = data.get(field_name, getattr(defaults, field_name))

        values["collect_probability"] = clamp_float(values["collect_probability"], 0.0, 1.0)
        values["auto_reply_probability"] = clamp_float(values["auto_reply_probability"], 0.0, 1.0)
        values["image_max_size_mb"] = max(float(values["image_max_size_mb"]), 0.1)
        values["max_images_per_day"] = max(int(values["max_images_per_day"]), 0)
        values["recent_image_cache_size"] = max(int(values["recent_image_cache_size"]), 1)
        values["panel_port"] = int(values["panel_port"])
        values["llm_min_interval_seconds"] = max(float(values["llm_min_interval_seconds"]), 0.0)
        values["match_threshold"] = max(float(values["match_threshold"]), 0.0)
        values["group_whitelist"] = normalize_str_list(values["group_whitelist"])
        values["group_blacklist"] = normalize_str_list(values["group_blacklist"])
        values["admin_users"] = normalize_str_list(values["admin_users"])
        return cls(**values)

    def group_allowed(self, group_id: str | None) -> bool:
        group = str(group_id or "")
        if not group:
            return False
        if self.group_whitelist and group not in self.group_whitelist:
            return False
        return group not in self.group_blacklist

    @property
    def max_image_bytes(self) -> int:
        return int(self.image_max_size_mb * 1024 * 1024)

    @property
    def panel_url(self) -> str:
        return f"http://{self.panel_host}:{self.panel_port}/?token={self.admin_token}"


def normalize_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def clamp_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = minimum
    return min(max(number, minimum), maximum)


def get_plugin_data_dir(plugin_name: str = PLUGIN_NAME) -> Path:
    """定位 data/plugin_data/{plugin_name}。

    AstrBot >= 4.9.2 可通过 get_astrbot_data_path 获取 data 目录；本地单独运行时
    回退到当前仓库下的 data/plugin_data，方便调试管理面板。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_root = Path(get_astrbot_data_path())
    except Exception:
        plugin_root = Path(__file__).resolve().parent
        if plugin_root.parent.name == "plugins":
            data_root = plugin_root.parent.parent
        else:
            data_root = plugin_root / "data"
    return data_root / "plugin_data" / plugin_name

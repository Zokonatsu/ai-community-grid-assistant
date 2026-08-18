"""
community_store.py
社区中心配置持久化模块

功能：
- 将后台「社区设置」保存的小区中心坐标/半径/名称写入 data/community_config.json。
- 读取时实时读盘（单节点、量小，无需缓存），改完立即生效、无需重启。
- 本模块为纯文件 IO，不 import geo.py / config.py，避免模块循环依赖。

配置结构：{name, center_lat, center_lng, radius_m, updated_at}
"""

import json
import os
from datetime import datetime

# 数据目录与配置文件路径（基于本模块绝对路径，服务从项目根目录运行时与 main.py 的 ./data 一致）
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
COMMUNITY_CONFIG_FILE = os.path.join(DATA_DIR, "community_config.json")


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load() -> dict | None:
    """
    读取社区中心配置。
    文件不存在或内容损坏时返回 None（调用方回退到环境变量默认值）。
    """
    if not os.path.exists(COMMUNITY_CONFIG_FILE):
        return None
    try:
        with open(COMMUNITY_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "center_lat" in data and "center_lng" in data:
            return data
        return None
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def save(name: str, center_lat: float, center_lng: float, radius_m: float) -> dict:
    """
    保存社区中心配置并返回落盘后的配置。
    调用方需保证参数已校验（lat∈[-90,90]、lng∈[-180,180]、radius_m>0）。
    """
    config = {
        "name": (name or "").strip() or "知·社区",
        "center_lat": float(center_lat),
        "center_lng": float(center_lng),
        "radius_m": float(radius_m),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _ensure_data_dir()
    try:
        with open(COMMUNITY_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise RuntimeError(f"保存社区设置失败：{exc}") from exc
    return config

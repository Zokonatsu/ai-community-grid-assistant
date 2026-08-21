"""
geo.py
定位与小区范围校验模块

功能：
- 使用 Haversine 公式计算两点球面距离（米）。
- 校验注册/事件坐标是否落在小区配置半径内。
- 生成高德地图标记链接，供后台展示位置。

小区中心点与半径通过环境变量覆盖（生产环境请配置真实值），
本模块自包含读取环境变量，不依赖 config.py，避免模块循环依赖。
"""

import math
import os

import community_store

# 小区定位配置（环境变量为兜底默认值；后台「社区设置」保存后优先使用持久化配置）
COMMUNITY_NAME: str = os.getenv("COMMUNITY_NAME", "知·社区")
COMMUNITY_CENTER_LAT: float = float(os.getenv("COMMUNITY_CENTER_LAT", "30.274150"))
COMMUNITY_CENTER_LNG: float = float(os.getenv("COMMUNITY_CENTER_LNG", "120.155150"))
COMMUNITY_RADIUS_M: float = float(os.getenv("COMMUNITY_RADIUS_M", "500"))


def get_community_config() -> dict:
    """
    获取当前生效的社区中心配置（含 name/center_lat/center_lng/radius_m）。
    后台保存过则用持久化值，否则回退到环境变量默认。
    """
    saved = community_store.load()
    if saved is not None:
        return saved
    return {
        "name": COMMUNITY_NAME,
        "center_lat": COMMUNITY_CENTER_LAT,
        "center_lng": COMMUNITY_CENTER_LNG,
        "radius_m": COMMUNITY_RADIUS_M,
    }


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """计算两点间球面距离（米），基于 Haversine 公式。"""
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def is_within_community(lat: float | None, lng: float | None) -> tuple[bool, float]:
    """
    校验坐标是否在小区半径内（中心点取当前生效配置）。
    返回 (是否在小区内, 距中心点米数)；坐标为 None 时返回 (False, -1.0)。
    """
    if lat is None or lng is None:
        return False, -1.0
    cfg = get_community_config()
    distance = haversine_meters(lat, lng, cfg["center_lat"], cfg["center_lng"])
    return distance <= cfg["radius_m"], distance


def amap_url(lat, lng, name: str = "") -> str:
    """
    生成高德地图标记链接（高德 uri 接口参数顺序为 position=lng,lat）。
    坐标为 None 时返回空字符串。
    """
    if lat is None or lng is None:
        return ""
    return (
        f"https://uri.amap.com/marker?position={lng},{lat}"
        f"&name={name or '位置'}&src=community-grid"
    )

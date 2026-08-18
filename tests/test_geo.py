"""
test_geo.py
geo 模块（小区范围校验）单元测试脚本

测试范围：
  1. haversine_meters 已知两点距离计算正确
  2. is_within_community 范围内/范围外/None 坐标
  3. amap_url 链接生成（坐标 / None）
"""

import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

import geo  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    RESULTS.append((name, cond, detail))


# --- 1. haversine_meters ---
# 纬度差 0.001° 约等于 111 米；允许 ±5 米误差
d = geo.haversine_meters(30.0, 120.0, 30.001, 120.0)
check("haversine 纬度0.001°≈111米", abs(d - 111.0) < 5.0, f"got={d:.1f}")

# 同一点距离为 0
d0 = geo.haversine_meters(30.0, 120.0, 30.0, 120.0)
check("haversine 同一点=0", abs(d0) < 0.001, f"got={d0:.4f}")

# 经度差 0.001°（赤道附近）约 111 米
dl = geo.haversine_meters(30.0, 120.0, 30.0, 120.001)
check("haversine 经度0.001°约111米", abs(dl - 96.0) < 10.0, f"got={dl:.1f}")

# --- 2. is_within_community ---
lat, lng = geo.COMMUNITY_CENTER_LAT, geo.COMMUNITY_CENTER_LNG
in_range, dist = geo.is_within_community(lat, lng)
check("中心点本身在范围内", in_range and dist == 0.0, f"in={in_range},dist={dist}")

# 中心点 + 2 倍半径距离
far = geo.COMMUNITY_RADIUS_M * 2.0 / 111000.0
out_range, dist_out = geo.is_within_community(lat + far, lng)
check("中心点+2倍半径在范围外", (not out_range) and dist_out > geo.COMMUNITY_RADIUS_M,
      f"in={out_range},dist={dist_out:.0f}")

# 半径内一点（100 米）
near = 100.0 / 111000.0
in_near, dist_near = geo.is_within_community(lat + near, lng)
check("中心点+100米在范围内", in_near and dist_near < geo.COMMUNITY_RADIUS_M,
      f"in={in_near},dist={dist_near:.0f}")

# None 坐标
n_in, n_dist = geo.is_within_community(None, None)
check("None坐标返回(False,-1.0)", (not n_in) and n_dist == -1.0, f"in={n_in},dist={n_dist}")

# --- 3. amap_url ---
url = geo.amap_url(lat, lng, "测试")
check("amap_url 含 position=lng,lat", url and f"position={lng},{lat}" in url, url)
check("amap_url 含 name", url and "name=测试" in url, url)
check("amap_url None返回空", geo.amap_url(None, lng) == "", geo.amap_url(None, lng))


def main():
    print("=" * 60)
    print("geo.py 单元测试")
    print("=" * 60)
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n结果：{len(RESULTS) - failed}/{len(RESULTS)} 通过")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

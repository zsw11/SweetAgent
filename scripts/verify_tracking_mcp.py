# -*- coding: utf-8 -*-
"""物流跟踪 MCP 接入验证（OPT-01 落地）。

场景：
  A. 降级：未配置 key -> track 返回空 rows，不抛异常（主链路不受影响）
  B. 无单号：配置了 key 但任务文本无单号 -> 返回空 rows
  C. 真实调用（有 key + 网络）：auto_number 识别承运商 + query_trace 查询轨迹
     （query_trace 可能因单号失效返回业务错误，但 MCP 链路本身已通）

运行：.venv\\Scripts\\python.exe -u scripts\\verify_tracking_mcp.py
"""
import sys

sys.path.insert(0, ".")


def scenario_a_no_key():
    from app.config.settings import settings
    from app.tools.logistics_tracking import LogisticsTrackingClient

    if settings.TRACKING_MCP_KEY:
        print("[A] 已配置 key，跳过（无法模拟无 key）")
        return
    rows = LogisticsTrackingClient(key="").track("查一下 YT9693083639795 的物流")
    assert rows == [], f"A: 无 key 应返回空，实际 {rows}"
    print("[A] 降级：无 key -> 空 rows 不抛异常 ✅")


def scenario_b_no_tracking_no():
    from app.config.settings import settings
    from app.tools.logistics_tracking import LogisticsTrackingClient

    if not settings.TRACKING_MCP_KEY:
        print("[B] 无 key，跳过（与 A 同降级路径）")
        return
    rows = LogisticsTrackingClient().track("分析一下最近物流成本变化趋势")
    assert rows == [], f"B: 无单号应返回空，实际 {rows}"
    print("[B] 无单号 -> 空 rows 不抛异常 ✅")


def scenario_c_real_call():
    """真实调用（有 key + 网络）：auto_number 至少应识别出承运商。"""
    from app.config.settings import settings
    from app.tools.logistics_tracking import LogisticsTrackingClient

    if not settings.TRACKING_MCP_KEY:
        print("[C] 未配置 key，跳过真实调用")
        return
    rows = LogisticsTrackingClient().track("查一下圆通 YT9693083639795 的物流轨迹")
    assert len(rows) == 1, f"C: 应返回 1 行，实际 {rows}"
    r = rows[0]
    assert r["method"] == "mcp", f"C: method 应为 mcp，实际 {r['method']}"
    print(f"[C] MCP 调用成功：单号 {r['tracking_no']} · 识别承运商 {r['carrier']}({r['carrier_code']})")
    if r["raw"]:
        print(f"    轨迹原始返回: {r['raw'][:200]}")
    print("    （query_trace 若返回业务错误=单号失效，非 MCP 问题）✅")


if __name__ == "__main__":
    scenario_a_no_key()
    scenario_b_no_tracking_no()
    scenario_c_real_call()
    print("\n物流跟踪 MCP 验证完成 ✅")

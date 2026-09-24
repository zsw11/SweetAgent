# -*- coding: utf-8 -*-
"""测试快递100 MCP：auto_number 识别公司 + query_trace 查询轨迹（json）。"""
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = "https://api.kuaidi100.com/mcp/streamable?key=uebKuhFT4730"
TRACKING_NO = "YT9693083639795"  # demo 测试单号（圆通）


async def call(session, tool, args):
    res = await session.call_tool(tool, args)
    for c in res.content:
        if c.type == "text":
            return c.text
    return str(res)


async def main():
    try:
        async with streamable_http_client(URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # 1) 单号识别公司
                print("=== auto_number ===")
                r1 = await call(session, "auto_number", {"kuaidiNum": TRACKING_NO, "responseFormat": "json"})
                print(r1[:600])
                # 2) 轨迹查询
                print("\n=== query_trace ===")
                r2 = await call(session, "query_trace", {"kuaidiNum": TRACKING_NO, "responseFormat": "json"})
                print(r2[:1500])
    except BaseExceptionGroup as eg:
        for e in eg.exceptions:
            print(f"❌ 子异常 {type(e).__name__}: {e}")
    except Exception as exc:
        print(f"❌ 失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())

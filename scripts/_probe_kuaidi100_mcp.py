# -*- coding: utf-8 -*-
"""探索快递100 MCP Server：连接 streamable 端点，list_tools 发现工具清单。"""
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = "https://api.kuaidi100.com/mcp/streamable?key=uebKuhFT4730"


async def main():
    try:
        async with streamable_http_client(URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("=== MCP 初始化成功 ===")
                tools = await session.list_tools()
                print(f"工具数量: {len(tools.tools)}")
                for t in tools.tools:
                    print(f"\n--- {t.name} ---")
                    print(f"描述: {t.description or ''}")
                    print(f"参数 Schema: {json.dumps(t.input_schema, ensure_ascii=False, indent=2)}")
    except BaseExceptionGroup as eg:
        for e in eg.exceptions:
            print(f"❌ 子异常 {type(e).__name__}: {e}")
    except Exception as exc:
        print(f"❌ 连接失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())

"""物流跟踪外部 MCP Client（快递100 · MCP streamable，OPT-01 落地）。

通过 MCP 协议连接快递100 物流查询 Server（https://api.kuaidi100.com/mcp/streamable），
动态发现并调用其工具：
- auto_number：快递单号 -> 可能的快递公司列表（自动识别承运商）
- query_trace：快递单号 -> 实时物流轨迹

设计要点：
1. **MCP 是"工具发现 + 调用"协议**：连接后先 initialize 协商，再按运行时拿到的
   input_schema 构造参数调用（tools/list / tools/call），不写死接口文档。
2. **返回结构与 SQL/RAG 同构**（requirement/rows/row_count/duration_ms），
   下游 _analyze 无感——与 knowledge 数据域同一模式。
3. **降级原则**：未配置 key / 网络失败 / 单号无结果，一律返回空 rows + warning，
   不影响 Agent 主链路（与"知识库未收录"同语义）。

安全：只读查询（轨迹/识别），无任何写操作；key 由 settings 注入（建议放 .env）。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Optional

from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("logistics_tracking")

# 快递单号：字母数字 10~32 位（覆盖国内/国际主流单号）
_TRACKING_NO_RE = re.compile(r"[A-Za-z0-9]{10,32}")


class LogisticsTrackingClient:
    """快递100 物流查询 MCP Client（每请求一连接，简单可靠）。"""

    def __init__(
        self,
        url: Optional[str] = None,
        key: Optional[str] = None,
        timeout: float = 20.0,
    ):
        self.key = (key if key is not None else settings.TRACKING_MCP_KEY or "").strip()
        base = url if url is not None else settings.TRACKING_MCP_URL
        self.url = base.format(key=self.key) if "{key}" in base else base
        self.timeout = timeout

    # ------------------------------------------------------------------
    # 公开入口：从任务文本提取单号并查询
    # ------------------------------------------------------------------
    def track(self, query_text: str) -> list[dict[str, Any]]:
        """解析任务文本中的快递单号 -> 识别承运商 -> 查询实时轨迹。

        返回与 KnowledgeRetriever 同构的 rows（method="mcp"）；
        无 key / 无单号 / 查询失败均降级为空列表。
        """
        if not self.key:
            logger.info("logistics.tracking.skipped", reason="no_api_key")
            return []
        number = self._extract_tracking_no(query_text)
        if not number:
            logger.info("logistics.tracking.skipped", reason="no_tracking_no", query=query_text[:100])
            return []
        try:
            return asyncio.run(self._run(number, query_text))
        except Exception as exc:
            logger.warning("logistics.tracking.fail", number=number, error=str(exc)[:200])
            return []

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_tracking_no(text: str) -> Optional[str]:
        """提取第一个形似快递单号的串（10~32 位字母数字）。"""
        m = _TRACKING_NO_RE.search(text or "")
        return m.group(0) if m else None

    async def _run(self, number: str, query_text: str) -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(self.url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # 1) 识别承运商（auto_number）
                carrier_code = ""
                carrier_name = ""
                try:
                    txt = await self._call_text(session, "auto_number", {"kuaidiNum": number, "responseFormat": "json"})
                    data = _parse_json(txt)
                    if data.get("data"):
                        first = data["data"][0]
                        carrier_code = str(first.get("comCode") or "")
                        carrier_name = str(first.get("name") or "")
                except Exception as exc:
                    logger.warning("logistics.tracking.auto_number_fail", number=number, error=str(exc)[:200])
                # 2) 查询轨迹（query_trace；顺丰/中通需手机号，未提供时由 server 提示）
                trace_text = ""
                try:
                    trace_text = await self._call_text(
                        session, "query_trace",
                        {"kuaidiNum": number, "responseFormat": "json"},
                    )
                except Exception as exc:
                    logger.warning("logistics.tracking.query_trace_fail", number=number, error=str(exc)[:200])
                logger.info(
                    "logistics.tracking.done",
                    number=number, carrier=carrier_code or "-",
                    has_trace=bool(trace_text), query=query_text[:80],
                )
                return [
                    {
                        "tracking_no": number,
                        "carrier_code": carrier_code,
                        "carrier": carrier_name,
                        "raw": trace_text,
                        "method": "mcp",
                        "confidence": "medium" if trace_text else "none",
                    }
                ]

    @staticmethod
    async def _call_text(session, tool: str, args: dict[str, Any]) -> str:
        """调用 MCP 工具并拼回文本结果。"""
        res = await session.call_tool(tool, args)
        parts = []
        for c in res.content:
            if getattr(c, "type", "") == "text":
                parts.append(c.text)
            else:
                parts.append(str(c))
        return "\n".join(parts)


def _parse_json(text: str) -> dict[str, Any]:
    """容错解析工具返回 JSON（部分 server 返回前带 ```json 包裹）。"""
    import json

    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {"data": obj}
    except Exception:
        return {}


def query_tracking(task: str) -> list[dict[str, Any]]:
    """便捷入口：单例逻辑（每次新建连接，简单可靠）。"""
    return LogisticsTrackingClient().track(task)


if __name__ == "__main__":
    # 自测：python app/tools/logistics_tracking.py "查一下 YT9693083639795 的物流"
    import sys

    demo = sys.argv[1] if len(sys.argv) > 1 else "查一下 YT9693083639795 的物流"
    t0 = time.perf_counter()
    rows = query_tracking(demo)
    print(f"耗时 {int((time.perf_counter() - t0) * 1000)}ms, rows={len(rows)}")
    for r in rows:
        print(r)

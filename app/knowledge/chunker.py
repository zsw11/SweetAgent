"""知识文档切分器：把长文档切成适合检索 + 喂 LLM 的 chunk。

策略（两级）：
1. 按段落（空行分隔）切分——段落是天然语义单元，优先保留完整段落；
2. 段落超长（> chunk_size）再按固定窗口切 + 重叠（overlap），
   避免把一句/一个知识点拦腰截断导致检索召回时上下文残缺。

设计要点：
- 窗口切分保证每个 chunk 长度可控（检索质量与 token 成本平衡）；
- 重叠保证跨窗口的语义衔接点不被漏掉（提问落在边界时仍能召回）；
- 纯函数、无 IO，方便单测与复现。
"""

from __future__ import annotations

import re

DEFAULT_CHUNK_SIZE = 500   # 单 chunk 目标字符数（中文约 500 字）
DEFAULT_OVERLAP = 50       # 窗口间重叠字符数

_SEP_RE = re.compile(r"\n\s*\n+")


def split_paragraphs(text: str) -> list[str]:
    """按空行切分成段落，剔除空段与纯空白。"""
    return [p.strip() for p in _SEP_RE.split(text or "") if p.strip()]


def split_window(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP) -> list[str]:
    """固定窗口切分 + 重叠（步长 = chunk_size - overlap）。"""
    if len(text) <= chunk_size:
        return [text]
    step = max(1, chunk_size - overlap)
    out: list[str] = []
    start = 0
    while start < len(text):
        out.append(text[start:start + chunk_size])
        start += step
    return out


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """文档切分入口：段落优先，超长段落窗口切分。返回 chunk 文本列表。"""
    chunks: list[str] = []
    for para in split_paragraphs(text):
        for piece in split_window(para, chunk_size=chunk_size, overlap=overlap):
            piece = piece.strip()
            if piece and piece not in chunks:
                chunks.append(piece)
    return chunks


def build_document_content(sections: dict[str, str]) -> str:
    """把 {小节标题: 正文} 拼成结构化文档文本（标题与正文之间用空行分隔，便于按段落切分）。

    例：{"一、市场趋势": "...", "二、竞品格局": "..."}
    → "一、市场趋势\n...\n\n二、竞品格局\n..."
    """
    parts: list[str] = []
    for title, body in sections.items():
        parts.append(f"{title}\n{body}")
    return "\n\n".join(parts)

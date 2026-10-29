"""所有 OpenAI / AsyncOpenAI 客户端都不许请求压缩响应 —— httpx2 2.16.0 的解压器一碰压缩就炸。

事故链：本机 openai==3.41.0 依赖 httpx2==2.16.0，后者 _decoders.py:191 给 C 解压器传了
关键字参数 output_buffer_limit=，而 process() 只收位置参数 → 只要服务端回压缩响应，每个
模型调用都 TypeError: process() takes no keyword arguments（用户看到的是"s0_module：API
调用失败（TypeError）"）；同一请求换成 Accept-Encoding: identity 立刻拿到真实 401，说明
传输层本身没问题。httpx2 2.16.0 已是最新版，没有上游修复。

所以约定只有一条：每个 OpenAI( / AsyncOpenAI( 构造点都带 default_headers，里面含
"Accept-Encoding": "identity"，不请求压缩就不走解压路径。这条测试用源码约定来钉它 ——
有人新增了一个调用点却忘了加头，"构造点个数 == 加了头的个数"立刻变红，而不是等课堂上
再炸一次。课堂侧两处（llm.py / phrases.py）还得把 identity **并进** provider 自带的头里
而不是替换：替换掉 OpenCode Go 的 x-opencode-session 会直接被 400。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: 仓库根：tests → classroom → TeachingFlow → 仓库根
ROOT = Path(__file__).resolve().parents[3]

#: 允许构造 OpenAI 客户端的文件（相对仓库根）。新增文件要补进这张表，否则这里管不到它。
CLIENT_FILES = (
    "TeachingFlow/core/llm.py",
    "PPT-Agent/agent.py",
    "PPT-Agent/pipeline.py",
    "集成路由V1.0/router.py",
    "TeachingFlow/classroom/llm.py",
    "TeachingFlow/classroom/phrases.py",
)

#: 这两处的头来自 provider（不许覆盖），块里必须有 ** 解包才算合并
MERGED_FILES = (
    "TeachingFlow/classroom/llm.py",
    "TeachingFlow/classroom/phrases.py",
)

#: 同时认 OpenAI( 与 AsyncOpenAI(：AsyncOpenAI( 里含子串 OpenAI(，所以把类名整体抓进组里
#: 再要求紧跟左括号 —— 只认 OpenAI( 的老写法会把 AsyncOpenAI( 整块漏掉（前面的 c 挡不住
#: 它就说明它根本不是 OpenAI），而放宽成前缀匹配又会把 AsyncOpenAI( 截成 OpenAI(。
CALL_RE = re.compile(r"(?<![A-Za-z0-9_])(AsyncOpenAI|OpenAI)")

#: 判定"加了头"的字面量，与源码里的写法逐字一致
HEADER = '"Accept-Encoding": "identity"'


def _blocks(rel: str) -> list[tuple[str, str]]:
    """切出文件里每个构造点的 (类名, 括号内文本)，按括号配对，不做语法分析。"""
    text = (ROOT / rel).read_text(encoding="utf-8")
    found: list[tuple[str, str]] = []
    for m in CALL_RE.finditer(text):
        if not text.startswith("(", m.end()):
            continue
        depth = 0
        for i in range(m.end(), len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    found.append((m.group(1), text[m.end() + 1:i]))
                    break
    return found


@pytest.mark.parametrize("rel", CLIENT_FILES)
def test_every_openai_client_asks_for_identity_encoding(rel: str) -> None:
    blocks = _blocks(rel)
    assert blocks, f"{rel} 里一个 OpenAI( / AsyncOpenAI( 都没有 —— 约定表过时了"
    for cls, body in blocks:
        assert HEADER in body, (
            f"{rel} 的 {cls}( 构造点没带 {HEADER}：压缩响应会让 httpx2 2.16.0 抛 "
            "TypeError: process() takes no keyword arguments"
        )
    print(f"[T1] {rel}: {len(blocks)} 个构造点都带 identity 头")


@pytest.mark.parametrize("rel", CLIENT_FILES)
def test_openai_client_count_equals_headered_count(rel: str) -> None:
    blocks = _blocks(rel)
    headered = [body for _, body in blocks if HEADER in body]
    assert len(blocks) == len(headered), (
        f"{rel}: 构造点有 {len(blocks)} 个，带头的只有 {len(headered)} 个 —— "
        f"新增调用点必须同时加 {HEADER}"
    )
    print(f"[T2] {rel}: 构造点个数 == 带头个数（{len(blocks)}）")


@pytest.mark.parametrize("rel", MERGED_FILES)
def test_identity_header_is_merged_not_replacing_provider_headers(rel: str) -> None:
    blocks = _blocks(rel)
    assert blocks, f"{rel} 里一个 AsyncOpenAI( 都没有 —— 合并表过时了"
    for cls, body in blocks:
        assert "**" in body and HEADER in body, (
            f"{rel} 的 {cls}( 是覆盖写而不是合并：identity 头必须用 ** 并进 provider 自带的"
            "头里，覆盖掉 x-opencode-session 会直接 400"
        )
    print(f"[T3] {rel}: identity 头是并进 provider 头的（** 与 {HEADER} 同时出现）")

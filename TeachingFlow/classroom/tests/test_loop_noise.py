"""事件循环噪声过滤 —— 只放过"对端重置"，别的异常一个都不许吞。

为什么钉这一条：
  实测（Windows + Proactor）浏览器播放/拖动首页那段 5.7 MB 的 poster 视频时，控制台会刷

      Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)
      ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。

  根因在 CPython：`_call_connection_lost` 对已经 RST 的 socket 又要 `shutdown(SHUT_RDWR)`，
  那一步没有 try（`Lib/asyncio/proactor_events.py`，源码里就带着 "XXX ... maybe using
  DisconnectEx() would be better"）。它**不是本服务的错**，服务照常在跑 ——
  但满屏堆栈会把真故障埋掉。

  反向的风险同样要钉住：如果"过滤噪声"变成"把循环异常全吞掉"，就等于关掉了
  "事件循环里有没人处理的异常"这条有用信号（test_acceptance_gaps.py 正是拿它当断言）。
  所以这里同时断言两件事：对端重置不刷栈、别的异常照样上报。

运行：  python tests\\test_loop_noise.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import _quiet_connection_resets  # noqa: E402


class FakeLoop:
    """只记下"有没有转交给默认处理器"，不真去打印堆栈。"""

    def __init__(self) -> None:
        self.forwarded: list[dict] = []

    def default_exception_handler(self, context: dict) -> None:
        self.forwarded.append(context)


def _reset_context() -> dict:
    """浏览器中途掐断连接时，CPython 报出来的那个 context（照抄实测）。"""
    return {
        "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
        "exception": ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"),
        "handle": SimpleNamespace(_callback="_ProactorBasePipeTransport._call_connection_lost"),
    }


def test_peer_reset_does_not_print_a_traceback() -> None:
    """对端重置是"浏览器中途断开"，正常现象，不该刷栈。"""
    loop = FakeLoop()
    _quiet_connection_resets(loop, _reset_context())
    assert loop.forwarded == [], "连接被对端重置不该再走默认处理器（那就是满屏堆栈的来源）"
    print("[N1] 对端重置的拆连接噪声被压掉")


def test_everything_else_still_reports() -> None:
    """别的异常必须原样上报 —— 否则等于把"循环里有未处理异常"这条信号关掉。"""
    loop = FakeLoop()
    context = {"message": "Task exception was never retrieved",
               "exception": ValueError("真的出事了")}
    _quiet_connection_resets(loop, context)
    assert loop.forwarded == [context], "循环里的其它异常必须照旧上报"
    print("[N2] 其它循环异常照旧上报")


def test_reset_without_exception_object_is_also_quiet() -> None:
    """`except` 分支之外还有"没有 exception 对象"的 context，不能因此崩在过滤器里。"""
    loop = FakeLoop()
    _quiet_connection_resets(loop, {"message": "Exception in callback ...", "exception": None})
    assert loop.forwarded == [{"message": "Exception in callback ...", "exception": None}], \
        "没有 exception 对象的 context 不能静默丢掉"
    print("[N3] 没有 exception 对象的 context 照旧上报")


ALL = [test_peer_reset_does_not_print_a_traceback,
       test_everything_else_still_reports,
       test_reset_without_exception_object_is_also_quiet]


def main() -> int:
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}\n     {e}")
    print(f"\n事件循环噪声结果：{len(ALL) - failed}/{len(ALL)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

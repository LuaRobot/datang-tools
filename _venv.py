"""共用兜底：系统 python3 缺依赖时，自动换到 nanobot 的 venv 解释器重跑自己。

用法（放在脚本最前面，import 第三方东西之前）：
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _venv; _venv.ensure()

为什么需要：本机 `python3` 是系统的，没有 aiohttp / nanobot；而 tools 下这些工具
文档写的是 `./xxx.py` 直接跑，不兜底就会 ModuleNotFoundError（2026-10-23 回归扫出来的）。

注意判「是否已在 venv 里」只能比绝对路径，不能比 realpath —— venv 的 bin/python
是软链到同一个系统解释器，realpath 会比出相等，然后就永远不切换。
"""
import os
import sys

# venv 解释器候选：先看环境变量（DATANG_VENV / NANOBOT_VENV 指向 venv 目录或 python 本体），
# 再依次试常见位置。
def _venv_candidates():
    out = []
    for env in ("DATANG_VENV", "NANOBOT_VENV"):
        v = os.environ.get(env)
        if not v:
            continue
        p = os.path.join(v, "bin", "python") if os.path.isdir(v) else v
        out.append(p)
    out.append(os.path.expanduser("~/.nanobot/venv/bin/python"))
    out.append(os.path.expanduser("~/venv/bin/python"))
    return tuple(out)


VENV_PYTHONS = _venv_candidates()


def _self():
    return os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else None


def ensure(guard=None):
    """缺依赖时 execv 到 venv python。guard 传模块名，能 import 就直接返回。"""
    if os.environ.get("_DATANG_VENV_REEXEC") == "1":
        return  # 已经换过一次，别再套娃
    ok = True
    if guard:
        try:
            __import__(guard)
        except ImportError:
            ok = False
    if ok:
        return
    me = _self()
    if not me or not os.path.exists(me):
        raise SystemExit(f"缺依赖 {guard}：找不到 venv，也定位不到自己的脚本路径")
    for cand in VENV_PYTHONS:
        if os.path.exists(cand) and os.path.abspath(cand) != os.path.abspath(sys.executable):
            os.environ["_DATANG_VENV_REEXEC"] = "1"
            os.execv(cand, [cand, me] + sys.argv[1:])
    raise SystemExit(f"缺依赖 {guard}：没找到可用的 venv python（可设 DATANG_VENV 指定）")

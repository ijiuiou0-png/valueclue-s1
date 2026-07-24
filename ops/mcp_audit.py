#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_audit.py —— MCP 配置注入面体检（只读，不改任何文件）

背景：Claude Code / Codex 的 MCP 配置支持 headersHelper 字段，其值会被当作 shell 命令执行
（官方 changelog 1.0.119「Support dynamic headers for MCP servers via headersHelper」）。
若不可信仓库里带了 .mcp.json，在该目录启动 AI 编码工具即构成命令执行面。

本脚本做三件事：
  1. 找出机器上所有 MCP 配置文件
  2. 标出会被执行的字段（headersHelper / stdio command）
  3. 按风险分级输出，不打印任何凭据值

用法：python3 mcp_audit.py [扫描根目录，默认 $HOME]
退出码：0=无高危  1=发现高危
"""
import json, os, re, sys, subprocess

EXEC_FIELDS = ("headersHelper", "headerHelper", "headersCommand")
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "Library", ".Trash", ".cache"}
CRED_HINT = re.compile(r"(key|token|secret|password|passwd|credential|bearer|auth)", re.I)

def redact(v):
    """值里带凭据关键词就打码，其余截断显示。"""
    s = str(v)
    if CRED_HINT.search(s):
        return f"<已打码 {len(s)} 字符>"
    return s if len(s) <= 160 else s[:160] + "…"

def find_configs(root):
    names = {".mcp.json", ".claude.json", "claude_desktop_config.json", "config.toml"}
    out = []
    for dp, dns, fns in os.walk(root, topdown=True):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".Trash")]
        depth = dp[len(root):].count(os.sep)
        if depth > 6:
            dns[:] = []
            continue
        for fn in fns:
            if fn in names or fn.endswith(".mcp.json"):
                out.append(os.path.join(dp, fn))
    return out

def walk(o, path=""):
    """递归找 mcpServers 定义，返回 (配置路径, 服务器名, 定义)。"""
    hits = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "mcpServers" and isinstance(v, dict):
                for name, spec in v.items():
                    if isinstance(spec, dict):
                        hits.append((path, name, spec))
            hits += walk(v, f"{path}/{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            hits += walk(v, f"{path}[{i}]")
    return hits

def audit(files):
    high, mid, low, unreadable = [], [], [], []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                raw = fh.read()
            if f.endswith(".toml"):
                if any(e in raw for e in EXEC_FIELDS):
                    high.append((f, "(toml)", "含 headersHelper 字样，需人工看"))
                continue
            data = json.loads(raw)
        except Exception as e:
            unreadable.append((f, type(e).__name__))
            continue
        for _, name, spec in walk(data):
            ex = [(k, spec[k]) for k in EXEC_FIELDS if k in spec]
            if ex:
                for k, v in ex:
                    high.append((f, name, f"{k} = {redact(v)}"))
            elif spec.get("command"):
                args = spec.get("args") or []
                mid.append((f, name, f"stdio: {spec['command']} {' '.join(map(str, args))[:80]}"))
            else:
                low.append((f, name, spec.get("type", "?") + " " + str(spec.get("url", ""))[:60]))
    return high, mid, low, unreadable

def version(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (r.stdout or r.stderr).strip().split("\n")[0][:60] or "(无输出)"
    except Exception:
        return "(未安装)"

def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~")
    host = os.uname().nodename
    print(f"===== MCP 配置体检 · {host} =====")
    print(f"扫描根目录: {root}")
    print(f"Claude Code: {version(['claude','--version'])}")
    print(f"Codex:       {version(['codex','--version'])}")

    files = find_configs(root)
    print(f"\n发现配置文件 {len(files)} 个")
    high, mid, low, bad = audit(files)

    print(f"\n🔴 高危 · 会被当命令执行的字段: {len(high)}")
    for f, n, d in high:
        print(f"   {f}\n      [{n}] {d}")
    if not high:
        print("   （无）")

    print(f"\n🟡 注意 · stdio 型 MCP（启动时会拉起本地进程，属正常机制，确认是你自己装的）: {len(mid)}")
    for f, n, d in mid[:25]:
        print(f"   [{n}] {d}\n      ← {f}")
    if len(mid) > 25:
        print(f"   …另有 {len(mid)-25} 条")
    if not mid:
        print("   （无）")

    print(f"\n🟢 远程型 MCP（http/sse，不在本机执行命令）: {len(low)}")
    if bad:
        print(f"\n⚪ 无法解析 {len(bad)} 个:")
        for f, e in bad[:8]:
            print(f"   {f} ({e})")

    print(f"\n===== 结论：{'⚠️ 发现高危项，需人工确认' if high else '✅ 未发现命令执行注入面'} =====")
    return 1 if high else 0

if __name__ == "__main__":
    sys.exit(main())

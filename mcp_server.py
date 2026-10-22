"""MCP server：把 tools.py 的 5 个工具暴露成一个 stdio 传输的 MCP server。

由 agent_core 作为子进程拉起，通过标准输入输出跑 JSON-RPC 2.0。

**stdio 下 stdout 是协议通道，绝对不能往里打印任何东西**——一句 print 就会
污染 JSON-RPC 流，客户端会直接解析失败。所以本文件（以及 tools.py）里没有
任何 print；要输出日志只能走 stderr，因为 stderr 不参与协议。

手动调试：
    python mcp_server.py            # 会静等 stdin 上的 JSON-RPC，看起来像卡住
用 .workbuddy/tmp/probe-mcp-server.py 这类客户端脚本去连它更直观。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import tools

server = FastMCP("agent-tools")

for _tool_function in tools.ALL_TOOLS:
    server.tool()(_tool_function)


if __name__ == "__main__":
    server.run(transport="stdio")

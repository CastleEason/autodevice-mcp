"""MCP server entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from mobile_auto_mcp.mcp_tools import register_all_tools  # noqa: E402


mcp = FastMCP(
    "mobile-auto-mcp",
    instructions=(
        "独立移动端异常场景执行 MCP：导入用例、启动代理、操作设备、截图并生成报告。"
        "需要进入目标页时必须先启动代理，再启动 App，再执行 UI 导航；UI 点击会先读取当前元素并解析候选，"
        "目标页必须通过页面断言后才允许执行接口探针和异常规则。"
        "独立 preflight 只读检测；正式 run_cases 会证明代理 Host 路由、托管设置并复核手机 WLAN 代理，"
        "执行后保留并提醒用户调用 restore_retained_proxy 安全恢复。内置视觉算法只做预检，最终结论必须显式复核。"
    ),
)

register_all_tools(mcp)


def main() -> None:
    """Run the MCP server in stdio mode."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()

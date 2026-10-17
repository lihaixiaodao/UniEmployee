"""Connector 层：CRM 连接器（FastMCP stdio server）。

数据与代码分离：本模块**不含任何硬编码业务数据**，订单与客户档案全部从外部
JSON 数据包加载，目录由 CRM_DATA_DIR 指定（默认 backend/seeddata/crm）。

企业接入方式（二选一，工具签名保持不变）：
  1. 用真实 CRM/ERP 导出的数据，替换 seeddata/crm 下的 orders.json / customers.json；
  2. 改造下方的 _load_dataset()，改为直连真实 CRM 接口/数据库。

数据包缺失或格式错误时本模块直接拒绝启动（fail-fast），绝不回退到内置假数据
——宁可让连接器不可用，也不能让数字员工拿编造的金额去对客答复。
"""
import json
import os
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("crm")

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "seeddata" / "crm"
CRM_DATA_DIR = Path(os.environ.get("CRM_DATA_DIR", "").strip() or _DEFAULT_DATA_DIR)


def _load_dataset(filename: str) -> dict:
    """加载一份 JSON 数据包，自动剔除 `_` 前缀的元数据键。"""
    path = CRM_DATA_DIR / filename
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"CRM 数据文件缺失：{path}。请配置 CRM_DATA_DIR 指向真实业务数据包目录"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"CRM 数据文件不是合法 JSON：{path}（第 {exc.lineno} 行第 {exc.colno} 列）"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"CRM 数据文件格式错误（顶层应为对象）：{path}")
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


ORDERS = _load_dataset("orders.json")
CUSTOMERS = _load_dataset("customers.json")


@mcp.tool
def order_query(order_id: str) -> dict:
    """按订单号查询订单详情（商品、金额、状态、签收日期）。"""
    return ORDERS.get(order_id, {"error": f"订单 {order_id} 不存在"})


@mcp.tool
def customer_profile(customer_name: str) -> dict:
    """按客户姓名查询客户档案（等级、历史订单）。"""
    return CUSTOMERS.get(customer_name, {"error": f"客户 {customer_name} 不存在"})


if __name__ == "__main__":
    mcp.run()  # stdio transport

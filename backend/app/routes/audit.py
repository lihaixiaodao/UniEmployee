"""管理端审计日志查询端点（仅 admin）。写入在各变更端点内调用 audit.log()。"""
from fastapi import APIRouter, Depends

from app import audit
from app import auth

router = APIRouter(prefix="/api/admin/audit")


@router.get("/logs")
async def list_audit_logs(limit: int = 50, offset: int = 0,
                          obj_type: str = "", actor_id: str = "",
                          action: str = "",
                          admin: dict = Depends(auth.require_admin)):
    # 分页参数上下界：审计页是管理员高频页面，不带约束的 limit 会被爬成全表扫描
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    logs, total = audit.list_logs(limit, offset, obj_type, actor_id, action,
                                  tenant_id=admin.get("tenant_id", "default"))
    return {"logs": logs, "total": total}

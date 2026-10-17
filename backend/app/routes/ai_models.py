"""AI 模型管理路由：CRUD + 设默认 + 测试连接。

所有接口仅 admin 可访问，变更操作记录审计日志。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import audit, catalog
from app.auth import require_admin, get_current_user
from app.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin/ai-models", dependencies=[Depends(require_admin)])


class ModelIn(BaseModel):
    """模型创建/更新入参。

    模型名与 base_model 是注入到 SDK 初始化参数的值，必须限长限字符集，
    避免把任意字符串喂给下游 provider。
    """
    name: str = Field(min_length=1, max_length=64)
    base_model: str = Field(min_length=1, max_length=128)
    supplier: int = Field(default=1, ge=1)
    model_type: int = Field(default=1, ge=1)
    api_key: str = Field(default="", max_length=512)
    api_domain: str = Field(default="", max_length=512)
    protocol: int = Field(default=1, ge=0)
    config_list: list = Field(default_factory=list)


class ModelUpdateIn(BaseModel):
    id: int
    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_model: str | None = Field(default=None, min_length=1, max_length=128)
    supplier: int | None = Field(default=None, ge=1)
    model_type: int | None = Field(default=None, ge=1)
    api_key: str | None = Field(default=None, max_length=512)
    api_domain: str | None = Field(default=None, max_length=512)
    protocol: int | None = Field(default=None, ge=0)
    config_list: list | None = None


class ModelTestIn(BaseModel):
    base_model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(default="", max_length=512)
    api_domain: str = Field(default="", max_length=512)


# 普通用户可访问的模型列表（不含 api_key），供对话页模型选择下拉框用
public_router = APIRouter(prefix="/api/ai-models")


@public_router.get("")
async def list_ai_models_public(user: dict = Depends(get_current_user)):
    models = catalog.list_models(model_type=1)  # 只返回 LLM 类型给对话页
    return [{
        "id": m["id"], "name": m["name"], "base_model": m["base_model"],
        "supplier": m["supplier"], "default_model": m["default_model"],
    } for m in models]


@router.get("")
async def list_ai_models(keyword: str = "", model_type: int = None):
    # api_key 一律掩码后出网：明文一旦进入响应，就会复制到前端日志与浏览器。
    return [catalog.with_masked_secret(m)
            for m in catalog.list_models(keyword=keyword, model_type=model_type)]


@router.get("/admission")
async def get_model_admission_stats():
    """管理员查看本进程模型请求准入、排队等待和拒绝计数。"""
    from app import model_admission
    return model_admission.stats()


@router.get("/{model_id}")
async def get_ai_model(model_id: int):
    m = catalog.get_model(model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    return catalog.with_masked_secret(m)


@router.post("")
async def create_ai_model(body: ModelIn, request: Request,
                          admin: dict = Depends(require_admin)):
    mid = catalog.create_model(body.model_dump())
    # 审计留痕同样脱敏：审计表会被导出、被查询、被更多人看到，
    # 密钥一旦写进去就再也收不回来。
    audit.log("create", "ai_model", str(mid), admin, request,
              after=catalog.with_masked_secret(catalog.get_model(mid)))
    return {"id": mid}


@router.put("")
async def update_ai_model(body: ModelUpdateIn, request: Request,
                          admin: dict = Depends(require_admin)):
    # 只提交本次实际携带的字段，避免用 None 覆盖掉未修改的项
    payload = body.model_dump(exclude_none=True, exclude={"id"})
    before = catalog.with_masked_secret(catalog.get_model(body.id))
    ok = catalog.update_model(body.id, payload)
    audit.log("update", "ai_model", str(body.id), admin, request,
              before=before,
              after=catalog.with_masked_secret(catalog.get_model(body.id)) if ok else None)
    return {"ok": ok}


@router.delete("/{model_id}")
async def delete_ai_model(model_id: int, request: Request,
                          admin: dict = Depends(require_admin)):
    before = catalog.with_masked_secret(catalog.get_model(model_id))
    ok = catalog.delete_model(model_id)
    audit.log("delete", "ai_model", str(model_id), admin, request, before=before)
    return {"ok": ok}


@router.put("/default/{model_id}")
async def set_default_ai_model(model_id: int, request: Request,
                               admin: dict = Depends(require_admin)):
    ok = catalog.set_default_model(model_id)
    if not ok:
        return {"error": "模型不存在"}
    audit.log("update", "ai_model", f"default:{model_id}", admin, request)
    return {"ok": ok}


@router.post("/status")
async def test_ai_model(body: ModelTestIn):
    """测试模型连接：用提交的配置发一条简单请求验证可达性。

    密钥仅用于本次连通性探测，不入日志、不入审计；失败时只回传异常类型，
    不回传原始报文——provider 的报错文本里经常夹带 URL、账号和请求头片段。
    """
    from langchain.chat_models import init_chat_model
    from langchain_core.messages import HumanMessage

    kwargs = {"use_responses_api": False} if body.api_domain else {}
    if body.api_key:
        kwargs["api_key"] = body.api_key
    if body.api_domain:
        kwargs["base_url"] = body.api_domain
    try:
        llm = init_chat_model(body.base_model, **kwargs)
        r = await llm.ainvoke([HumanMessage(content="回复OK")])
    except Exception as e:
        logger.warning("模型连通性测试失败：base_model=%s 异常=%s",
                       body.base_model, type(e).__name__)
        return {"success": False,
                "message": f"连接失败（{type(e).__name__}），请检查模型标识、API 域名与密钥"}
    return {"success": True, "message": "连接成功", "reply": str(r.content)[:200]}

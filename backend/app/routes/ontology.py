"""业务本体管理 API：schema（实体/关系类型）与 data（实例）的 CRUD。

仅管理员可访问，数据按登录用户的 tenant_id 隔离。

本体里存的是企业业务事实（客户、合同、项目、设备及其关系），数字员工会
基于它回答业务问题，也会被 AI 对话写回。因此所有写操作一律留审计痕迹，
否则「这条客户记录是谁改的」在事后无从回答。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import auth, ontology, audit

router = APIRouter(prefix="/api/admin/ontology", dependencies=[Depends(auth.require_admin)])


def _tenant(admin: dict) -> str:
    return admin.get("tenant_id", "default")


def _ok(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---- 入参模型 ----
# extra="allow"：本体允许各企业自定义属性，模型只守住数据层要求的必备项与长度
# 上限，不把业务自定义属性挡在门外。字段名与 ontology 数据层契约严格对齐。
class _ExtraAllowed(BaseModel):
    model_config = {"extra": "allow"}


class EntityTypeCreateIn(_ExtraAllowed):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)


class EntityTypeUpdateIn(_ExtraAllowed):
    name: str = Field(min_length=1, max_length=64)


class RelationTypeCreateIn(_ExtraAllowed):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)


class RelationTypeUpdateIn(_ExtraAllowed):
    name: str = Field(min_length=1, max_length=64)


class EntityIn(_ExtraAllowed):
    entity_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)


class RelationCreateIn(_ExtraAllowed):
    from_id: int
    to_id: int
    relation_type: str = Field(min_length=1, max_length=64)


# ---- Schema ----

@router.get("/schema")
async def get_schema(admin: dict = Depends(auth.require_admin)):
    return ontology.list_schema(_tenant(admin))


@router.post("/entity-types")
async def create_entity_type(body: EntityTypeCreateIn, request: Request,
                             admin: dict = Depends(auth.require_admin)):
    payload = body.model_dump()
    type_id = _ok(ontology.create_entity_type, _tenant(admin), payload)
    audit.log("create", "ontology_entity_type", str(type_id), admin, request, after=payload)
    return {"id": type_id}


@router.put("/entity-types/{type_id}")
async def update_entity_type(type_id: int, body: EntityTypeUpdateIn, request: Request,
                             admin: dict = Depends(auth.require_admin)):
    payload = body.model_dump()
    _ok(ontology.update_entity_type, _tenant(admin), type_id, payload)
    audit.log("update", "ontology_entity_type", str(type_id), admin, request, after=payload)
    return {"ok": True}


@router.delete("/entity-types/{type_id}")
async def delete_entity_type(type_id: int, request: Request,
                             admin: dict = Depends(auth.require_admin)):
    _ok(ontology.delete_entity_type, _tenant(admin), type_id)
    audit.log("delete", "ontology_entity_type", str(type_id), admin, request)
    return {"ok": True}


@router.post("/relation-types")
async def create_relation_type(body: RelationTypeCreateIn, request: Request,
                               admin: dict = Depends(auth.require_admin)):
    payload = body.model_dump()
    type_id = _ok(ontology.create_relation_type, _tenant(admin), payload)
    audit.log("create", "ontology_relation_type", str(type_id), admin, request, after=payload)
    return {"id": type_id}


@router.put("/relation-types/{type_id}")
async def update_relation_type(type_id: int, body: RelationTypeUpdateIn, request: Request,
                               admin: dict = Depends(auth.require_admin)):
    payload = body.model_dump()
    _ok(ontology.update_relation_type, _tenant(admin), type_id, payload)
    audit.log("update", "ontology_relation_type", str(type_id), admin, request, after=payload)
    return {"ok": True}


@router.delete("/relation-types/{type_id}")
async def delete_relation_type(type_id: int, request: Request,
                               admin: dict = Depends(auth.require_admin)):
    _ok(ontology.delete_relation_type, _tenant(admin), type_id)
    audit.log("delete", "ontology_relation_type", str(type_id), admin, request)
    return {"ok": True}


# ---- Data ----

@router.get("/entities")
async def list_entities(entity_type: str | None = None, keyword: str | None = None,
                        admin: dict = Depends(auth.require_admin)):
    return {"items": ontology.list_entities(
        _tenant(admin), entity_type=entity_type, keyword=keyword, limit=500)}


@router.post("/entities")
async def create_entity(body: EntityIn, request: Request,
                        admin: dict = Depends(auth.require_admin)):
    payload = body.model_dump()
    entity_id = _ok(ontology.create_entity, _tenant(admin), payload)
    audit.log("create", "ontology_entity", str(entity_id), admin, request, after=payload)
    return {"id": entity_id}


@router.get("/entities/{entity_id}")
async def get_entity(entity_id: int, admin: dict = Depends(auth.require_admin)):
    e = ontology.get_entity(_tenant(admin), entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="实体不存在")
    e["relations"] = ontology.list_relations(_tenant(admin), entity_id)
    return e


@router.put("/entities/{entity_id}")
async def update_entity(entity_id: int, body: EntityIn, request: Request,
                        admin: dict = Depends(auth.require_admin)):
    before = ontology.get_entity(_tenant(admin), entity_id)
    payload = body.model_dump()
    _ok(ontology.update_entity, _tenant(admin), entity_id, payload)
    audit.log("update", "ontology_entity", str(entity_id), admin, request,
              before=before, after=payload)
    return {"ok": True}


@router.delete("/entities/{entity_id}")
async def delete_entity(entity_id: int, request: Request,
                        admin: dict = Depends(auth.require_admin)):
    # 先取快照再删：实体删除是软删除，但审计要在删之前拿到内容，
    # 否则事后只能看到「某 id 被删了」，说不出删的是什么。
    before = ontology.get_entity(_tenant(admin), entity_id)
    _ok(ontology.delete_entity, _tenant(admin), entity_id)
    audit.log("delete", "ontology_entity", str(entity_id), admin, request, before=before)
    return {"ok": True}


@router.get("/relations")
async def list_relations(entity_id: int | None = None,
                         admin: dict = Depends(auth.require_admin)):
    return {"items": ontology.list_relations(_tenant(admin), entity_id)}


@router.get("/graph")
async def expand_graph(
    entity_id: int,
    # 图遍历成本随深度指数增长，不带上限的 depth/limit 足以把实例拖垮
    depth: int = Query(default=2, ge=1, le=5),
    relation_types: list[str] | None = None,
    limit: int = Query(default=80, ge=1, le=500),
    admin: dict = Depends(auth.require_admin),
):
    return _ok(ontology.expand_entity, _tenant(admin), entity_id,
               depth=depth, relation_types=relation_types, limit=limit)


@router.get("/paths")
async def find_paths(
    source_id: int,
    target_id: int | None = None,
    target_type: str | None = None,
    max_depth: int = Query(default=3, ge=1, le=6),
    relation_types: list[str] | None = None,
    limit: int = Query(default=10, ge=1, le=100),
    admin: dict = Depends(auth.require_admin),
):
    return {"items": _ok(
        ontology.find_paths, _tenant(admin), source_id,
        target_id=target_id, target_type=target_type, max_depth=max_depth,
        relation_types=relation_types, limit=limit)}


@router.post("/relations")
async def create_relation(body: RelationCreateIn, request: Request,
                          admin: dict = Depends(auth.require_admin)):
    payload = body.model_dump()
    relation_id = _ok(ontology.create_relation, _tenant(admin), payload)
    audit.log("create", "ontology_relation", str(relation_id), admin, request, after=payload)
    return {"id": relation_id}


@router.delete("/relations/{relation_id}")
async def delete_relation(relation_id: int, request: Request,
                          admin: dict = Depends(auth.require_admin)):
    _ok(ontology.delete_relation, _tenant(admin), relation_id)
    audit.log("delete", "ontology_relation", str(relation_id), admin, request)
    return {"ok": True}


@router.get("/stats")
async def get_stats(admin: dict = Depends(auth.require_admin)):
    return ontology.stats(_tenant(admin))


@router.get("/scenarios/customer-360")
async def customer_360(customer_name: str = Query(min_length=1, max_length=128),
                       admin: dict = Depends(auth.require_admin)):
    return _ok(ontology.customer_360, _tenant(admin), customer_name)


@router.get("/scenarios/fault-impact")
async def fault_impact(station_name: str = Query(min_length=1, max_length=128),
                       admin: dict = Depends(auth.require_admin)):
    return _ok(ontology.fault_impact, _tenant(admin), station_name)

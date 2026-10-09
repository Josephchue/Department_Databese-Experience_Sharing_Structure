"""经验知识库：K1 录入 / K2 语义检索 / K3 元信息。

这一条线**不碰 MySQL**——经验本体存在 Chroma 里。
和 MySQL 的联系只靠 id：元信息里记 author_id / activity_id /
participation_id，这些 id 和 MySQL 的 member.id / activity.id /
participation.id 是同一套。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from .. import db, models, schemas
from .. import vectors as vec

router = APIRouter(prefix="/api/experience", tags=["经验知识库 K1-K3"])

# vec.list_all 是「取全量 → 排序 → 切片」，所以传一个够大的 limit 就等于取全量。
# 筛选要在切片**之前**做：反过来的话，第 1 页 50 条被筛掉只剩 3 条，
# 第 2 页却从第 51 条开始，中间那 47 条谁也看不到——分页会静默漏数据。
_FETCH_ALL = 10 ** 9


def _split_tags(metadata: dict) -> dict:
    """Chroma 的 metadata 只能存标量，入库时把标签拼成了逗号串。

    返回给前端时再拆回列表，前端好渲染。
    """
    meta = dict(metadata)
    tags = meta.get("tags")
    if isinstance(tags, str):
        meta["tags"] = [t for t in tags.split(",") if t]
    elif tags is None:
        meta["tags"] = []
    return meta


@router.post("", response_model=schemas.ExperienceOut, summary="K1 录入经验")
def create_experience(payload: schemas.ExperienceCreate, session: Session = Depends(db.get_db)):
    """录入一条经验。

    除了写向量库，还会做两件事：
      1. 校验 author_id / activity_id 在 MySQL 里真实存在（防止造出孤儿引用）
      2. 如果带了 participation_id，把新经验的 id 追加进那条参与记录的 exp_ids 里
         —— 这就是 schema 里 exp_ids 字段的用途
    """
    if payload.type not in schemas.EXPERIENCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type 只能是 {'/'.join(schemas.EXPERIENCE_TYPES)}",
        )

    if payload.author_id is not None:
        if session.get(models.Member, payload.author_id) is None:
            raise HTTPException(status_code=400, detail=f"author_id={payload.author_id} 不存在")
    if payload.activity_id is not None:
        if session.get(models.Activity, payload.activity_id) is None:
            raise HTTPException(status_code=400, detail=f"activity_id={payload.activity_id} 不存在")

    participation = None
    if payload.participation_id is not None:
        participation = session.get(models.Participation, payload.participation_id)
        if participation is None:
            raise HTTPException(status_code=400, detail=f"participation_id={payload.participation_id} 不存在")

    exp_id = vec.next_id()
    metadata = {
        "type": payload.type,
        "tags": payload.tags,
        "author_id": payload.author_id,
        "activity_id": payload.activity_id,
        "participation_id": payload.participation_id,
        "source": payload.source,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    vec.add(exp_id, payload.content, metadata)

    # 反向关联：参与记录 → 经验
    if participation is not None:
        linked = list(participation.exp_ids or [])
        linked.append(exp_id)
        participation.exp_ids = linked
        session.commit()

    return {"exp_id": exp_id, "content": payload.content, "metadata": _split_tags(vec.get(exp_id)["metadata"])}


@router.get("/search", response_model=list[schemas.SearchHit], summary="K2 语义检索")
def search_experience(
    q: str = Query(..., min_length=1, description="自然语言问句，如「设备出故障了咋办」"),
    top_k: int = Query(5, ge=1, le=20),
):
    """用自然语言召回相关经验。

    这是整个 demo 的核心。注意它是**语义**检索：
    问「设备出故障了咋办」能召回「投影仪不亮」那条，
    但按关键词搜「设备」一条都搜不到——两者字面上没有共同词。
    """
    hits = vec.search(q, top_k=top_k)
    for hit in hits:
        hit["metadata"] = _split_tags(hit["metadata"])
    return hits


@router.get("/keyword", response_model=list[schemas.KeywordHit], summary="关键词对照（非向量检索）")
def keyword_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
):
    """字面包含查找，用来和上面的语义检索做对照。

    同一个问句「设备出故障了咋办」丢给这个接口，会返回 0 条；
    丢给 /search 却能召回「投影仪不亮」。
    这就是为什么经验库要用向量检索——大家记笔记时的用词根本对不上。
    """
    hits = vec.keyword_search(q, limit=limit)
    for hit in hits:
        hit["metadata"] = _split_tags(hit["metadata"])
    return hits


@router.get("", response_model=list[schemas.ExperienceOut], summary="经验列表（筛选 + 分页）")
def list_experiences(
    response: Response,
    exp_type: str | None = Query(None, alias="type", description="按类型筛选，取值见 /api/meta"),
    tag: str | None = Query(None, description="按标签筛选（命中其中一个就算，精确匹配）"),
    author_id: int | None = Query(None, description="按 author_id 筛选"),
    without_tags: bool = Query(False, description="只看还没打标签的（K8 打标前先看这批）"),
    q: str | None = Query(None, description="正文**字面**包含，不看语义（语义检索走 /search）"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """列经验，可筛选。

    ★ 参数名用 exp_type + alias="type"：直接叫 type 会遮蔽内置的 type()，
      传 alias 后前端/文档里仍然写 ?type=问题。

    ★ 筛选结果总数放在 X-Total-Count 响应头里，**不是**放在 JSON 里：
      返回体已经是 list[ExperienceOut]，套一层 {"total":…, "items":…}
      会破坏 K9 前端已经在用的形状。分页器要的是筛选后的总数，
      不是全库总数——否则末页会翻出一堆空页。

    TODO 条目过千要改用 Chroma 的 where 过滤：现在是把全量拉进内存再筛，
    几百条没问题，上万条就会明显变慢（每次请求都要读一遍整个集合）。
    """
    items = vec.list_all(limit=_FETCH_ALL, offset=0)

    def keep(item: dict) -> bool:
        meta = item["metadata"]
        if exp_type and meta.get("type") != exp_type:
            return False
        if author_id is not None and meta.get("author_id") != author_id:
            return False
        # 注意是「有没有标签」而不是「标签是不是空串」：
        # Chroma 里没打过标签的条目压根没有 tags 这个 key（_clean_metadata 会丢掉空值）。
        has_tags = bool(_split_tags(meta).get("tags"))
        if without_tags and has_tags:
            return False
        if tag and tag not in _split_tags(meta).get("tags", []):
            return False
        if q and q.lower() not in item["content"].lower():
            return False
        return True

    # 先筛后切（顺序不能反，理由见文件头的 _FETCH_ALL 注释）
    matched = [item for item in items if keep(item)]
    response.headers["X-Total-Count"] = str(len(matched))

    page = matched[offset:offset + limit]
    for item in page:
        item["metadata"] = _split_tags(item["metadata"])
    return page


@router.get("/{exp_id}", response_model=schemas.ExperienceOut, summary="K3 经验元信息")
def get_experience(exp_id: str, session: Session = Depends(db.get_db)):
    """按 id 取单条经验，并把 author_id / activity_id 翻译成人能看懂的名字。

    经验本体不在 MySQL 里，所以这里要拿元信息里的 id 回查 MySQL 补名称。
    这正是需求文档说的「双库并存，通过 id 关联」。
    """
    item = vec.get(exp_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")

    metadata = _split_tags(item["metadata"])

    author_id = metadata.get("author_id")
    if author_id:
        member = session.get(models.Member, int(author_id))
        metadata["author_name"] = member.name if member else None

    activity_id = metadata.get("activity_id")
    if activity_id:
        activity = session.get(models.Activity, int(activity_id))
        metadata["activity_name"] = activity.name if activity else None

    return {"exp_id": item["exp_id"], "content": item["content"], "metadata": metadata}


@router.delete("/{exp_id}", summary="删除经验")
def delete_experience(exp_id: str):
    if vec.get(exp_id) is None:
        raise HTTPException(status_code=404, detail=f"经验 {exp_id} 不存在")
    vec.delete(exp_id)
    return {"ok": True, "deleted": exp_id}

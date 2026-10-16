'''web/api/rule_service.py'''

'''
规则与示范条款接口（SPEC 7.3）

    GET    /api/rules          规则树列表（按 contract_type 分组）
    POST   /api/rules          新增规则
    PUT    /api/rules/{id}     编辑规则
    DELETE /api/rules/{id}     删除规则
    GET    /api/templates      示范条款列表
    POST   /api/templates      新增示范条款
    PUT    /api/templates/{id} 编辑示范条款
    DELETE /api/templates/{id} 删除示范条款

三个必须处理好的点:
1. **改完规则要清缓存**：`node_rule_match` 把启用规则缓存在内存里（TTL 默认 60s，
   SPEC 9.1 的 RuleRegistry）。管理员改完规则若不清缓存，要等最多 60 秒才生效 ——
   在页面上就是"我明明改了怎么没生效"。所以每次写操作都调 `reset_rule_cache()`。
2. **删规则要提示影响面**：`risk_item.matched_rule_id` 是 SET NULL，
   删掉规则后历史风险项的依据会丢失。响应里把"有多少风险项引用了它"告诉管理员。
3. **条件树要做基本校验**：SPEC 9.2 的算子集是封闭的，配错算子会在审查时被静默跳过
   （规则引擎的容错设计）。在写入时就拦住，比事后看日志找漏检要好得多。
'''

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from db.constants import ClauseType, RiskDimension, RiskLevel, UserRole
from db.models import RiskItem, Rule, TemplateClause
from db.session import session_scope
from processor.review_processor.nodes.node_rule_match import reset_rule_cache
from web.deps import get_current_user, require_roles
from web.errors import ApiError, ErrorCode, ok, paged
from web.schemas import (
    ListQuery,
    RuleCreateRequest,
    RuleUpdateRequest,
    TemplateCreateRequest,
    TemplateUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api', tags=['规则与模板'])

# 读：法务也需要看规则才能理解风险依据；写：仅管理员
_CAN_READ = require_roles(UserRole.LEGAL, UserRole.ADMIN)
_CAN_WRITE = require_roles(UserRole.ADMIN)

# SPEC 9.2 的算子集 + 我们扩展的 not_regex（规则引擎实际支持的全部算子）
SUPPORTED_OPERATORS = frozenset({
    'eq', 'contains', 'contains_any', 'regex', 'not_regex', 'not_contains',
    'exists', 'missing', 'gt', 'lt',
})
# 逻辑节点
LOGIC_KEYS = frozenset({'and', 'or', 'not'})
# 信封字段（SPEC 之外的扩展，见 db/seed.py 的说明）
ENVELOPE_KEYS = frozenset({'scope', 'risk_type', 'when'})


def _rule_out(row: Rule, reference_count: int = 0) -> Dict[str, Any]:
    """规则输出结构"""
    return {
        'id': row.id,
        'code': row.code,
        'name': row.name,
        'contract_type': row.contract_type or '*',
        'dimension': row.dimension or '',
        'trigger_condition': row.trigger_condition or {},
        'risk_level': row.risk_level or '',
        'legal_basis': row.legal_basis or '',
        'template_clause_id': row.template_clause_id,
        'enabled': bool(row.enabled),
        'priority': row.priority or 0,
        'reference_count': reference_count,
        'created_at': row.created_at.isoformat() if row.created_at else '',
        'updated_at': row.updated_at.isoformat() if row.updated_at else '',
    }


def _template_out(row: TemplateClause, reference_count: int = 0) -> Dict[str, Any]:
    """示范条款输出结构"""
    return {
        'id': row.id,
        'contract_type': row.contract_type or '*',
        'clause_type': row.clause_type or '',
        'title': row.title or '',
        'content': row.content or '',
        'enabled': bool(row.enabled),
        'reference_count': reference_count,
        'created_at': row.created_at.isoformat() if row.created_at else '',
    }


def validate_condition_tree(condition: Any, path: str = 'root') -> List[str]:
    """
    校验触发条件树（递归）

    能配错的地方有三种，全部提前拦住：
      · 未知算子        —— 引擎会静默跳过该规则，等于规则没生效
      · 正则非法        —— 同上
      · 结构不对        —— 比如 and 后面不是列表
    :param condition: 条件树（或条件树的一部分）
    :param path: 当前路径（用于报错定位）
    :return: 错误信息列表（为空表示合法）
    """
    import re

    errors: List[str] = []

    if condition is None:
        return [f'{path}: 条件为空']

    if not isinstance(condition, dict):
        return [f'{path}: 应为对象，实际 {type(condition).__name__}']

    # 逻辑节点
    logic_keys = LOGIC_KEYS.intersection(condition.keys())
    if logic_keys:
        for key in logic_keys:
            value = condition[key]
            if key == 'not':
                errors.extend(validate_condition_tree(value, f'{path}.not'))
                continue
            if not isinstance(value, list) or not value:
                errors.append(f'{path}.{key}: 应为非空数组')
                continue
            for index, child in enumerate(value):
                errors.extend(validate_condition_tree(child, f'{path}.{key}[{index}]'))
        return errors

    # 叶子条件：必须带 field / op
    if 'field' not in condition:
        errors.append(f'{path}: 叶子条件缺少 field')
    if 'op' not in condition:
        errors.append(f'{path}: 叶子条件缺少 op')
        return errors

    operator = condition.get('op')
    if operator not in SUPPORTED_OPERATORS:
        errors.append(
            f'{path}.op: 不支持的算子 {operator!r}，可选 {sorted(SUPPORTED_OPERATORS)}'
        )
        return errors

    if operator in ('regex', 'not_regex'):
        pattern = condition.get('value')
        if not isinstance(pattern, str):
            errors.append(f'{path}.value: {operator} 的 value 应为字符串正则')
        else:
            try:
                re.compile(pattern)
            except re.error as e:
                errors.append(f'{path}.value: 正则非法（{e}）')

    if operator == 'contains_any':
        value = condition.get('value')
        if not isinstance(value, list) or not value:
            errors.append(f'{path}.value: contains_any 的 value 应为非空数组')

    return errors


def _validate_envelope(trigger_condition: Dict[str, Any]) -> None:
    """
    校验规则条件的整体结构（信封 + 条件树，或 SPEC 9.2 的简写形式）
    :raises ApiError: 4001
    """
    if not isinstance(trigger_condition, dict) or not trigger_condition:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 'trigger_condition 必须是非空对象')

    keys = set(trigger_condition.keys())
    if keys & LOGIC_KEYS:
        # 简写形式：整棵树就是条件树
        tree = trigger_condition
    elif 'when' in keys:
        unknown = keys - ENVELOPE_KEYS
        if unknown:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                f'trigger_condition 含未知字段 {sorted(unknown)}，'
                f'信封只允许 {sorted(ENVELOPE_KEYS)}',
            )
        tree = trigger_condition['when']
    else:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "trigger_condition 必须是条件树（含 and/or/not）或信封形式（含 when 字段）",
        )

    errors = validate_condition_tree(tree)
    if errors:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            '触发条件非法：' + '；'.join(errors[:5]),
            data={'errors': errors},
        )


# ============================================================
# 规则
# ============================================================
@router.get('/rules', summary='规则列表（可按合同类型分组）')
def list_rules(
    query: ListQuery = Depends(),
    group_by_contract_type: bool = Query(default=True, description='是否按合同类型分组'),
    current_user: Dict[str, Any] = Depends(_CAN_READ),
) -> Dict[str, Any]:
    """
    规则列表

    SPEC 7.3 要求「规则树列表（按 contract_type 分组）」，所以默认返回分组结构；
    关掉分组则返回普通分页列表。
    """
    conditions = []
    if query.contract_type:
        conditions.append(Rule.contract_type == query.contract_type)
    if query.keyword:
        like = f'%{query.keyword.strip()}%'
        conditions.append(Rule.name.like(like) | Rule.code.like(like))

    with session_scope() as session:
        count_stmt = select(func.count()).select_from(Rule)
        rows_stmt = select(Rule).order_by(Rule.contract_type, Rule.priority, Rule.code)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
            rows_stmt = rows_stmt.where(*conditions)

        total = session.scalar(count_stmt) or 0
        if group_by_contract_type:
            rows = session.scalars(rows_stmt).all()
        else:
            rows = session.scalars(
                rows_stmt.offset((query.page - 1) * query.page_size).limit(query.page_size)
            ).all()

        # 引用计数（删之前要能告诉管理员影响面）
        ref_rows = session.execute(
            select(RiskItem.matched_rule_id, func.count())
            .where(RiskItem.matched_rule_id.is_not(None))
            .group_by(RiskItem.matched_rule_id)
        ).all()
        ref_map = {rule_id: count for rule_id, count in ref_rows}
        items = [_rule_out(row, ref_map.get(row.id, 0)) for row in rows]

    if not group_by_contract_type:
        return paged(items, total=total, page=query.page, page_size=query.page_size)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item['contract_type'], []).append(item)
    # 通配（*）排最前，其余按名称排序，保证前端展示顺序稳定
    grouped = [
        {'contract_type': key, 'rules': groups[key]}
        for key in sorted(groups.keys(), key=lambda k: (k != '*', k))
    ]
    return ok({'total': total, 'groups': grouped})


@router.post('/rules', status_code=201, summary='新增规则')
def create_rule(payload: RuleCreateRequest,
                current_user: Dict[str, Any] = Depends(_CAN_WRITE)) -> Dict[str, Any]:
    """新增规则（条件树会先做结构与算子校验）"""
    _validate_envelope(payload.trigger_condition)

    if payload.risk_level not in RiskLevel.ALL:
        raise ApiError(ErrorCode.VALIDATION_FAILED, f'非法风险等级 {payload.risk_level!r}')
    if payload.dimension and payload.dimension not in RiskDimension.ALL:
        raise ApiError(ErrorCode.VALIDATION_FAILED, f'非法审查维度 {payload.dimension!r}')

    with session_scope() as session:
        if session.scalars(select(Rule).where(Rule.code == payload.code)).first() is not None:
            raise ApiError(ErrorCode.CONFLICT, f'规则编号 {payload.code} 已存在', http_status=409)

        if payload.template_clause_id is not None:
            if session.get(TemplateClause, payload.template_clause_id) is None:
                raise ApiError(ErrorCode.NOT_FOUND,
                               f'示范条款 {payload.template_clause_id} 不存在', http_status=404)

        row = Rule(
            code=payload.code, name=payload.name, contract_type=payload.contract_type,
            dimension=payload.dimension or None,
            trigger_condition=payload.trigger_condition,
            risk_level=payload.risk_level, legal_basis=payload.legal_basis,
            template_clause_id=payload.template_clause_id,
            enabled=payload.enabled, priority=payload.priority,
        )
        session.add(row)
        session.flush()
        result = _rule_out(row)

    reset_rule_cache()  # 规则改了必须立刻让引擎看到
    logger.info(f'[接入] 新增规则 {payload.code}（操作人 {current_user["username"]}）')
    return ok(result, message='规则已创建')


@router.put('/rules/{rule_id}', summary='编辑规则')
def update_rule(rule_id: int, payload: RuleUpdateRequest,
                current_user: Dict[str, Any] = Depends(_CAN_WRITE)) -> Dict[str, Any]:
    """编辑规则（只改传了的字段）"""
    if payload.trigger_condition is not None:
        _validate_envelope(payload.trigger_condition)
    if payload.risk_level is not None and payload.risk_level not in RiskLevel.ALL:
        raise ApiError(ErrorCode.VALIDATION_FAILED, f'非法风险等级 {payload.risk_level!r}')
    if payload.dimension is not None and payload.dimension not in RiskDimension.ALL:
        raise ApiError(ErrorCode.VALIDATION_FAILED, f'非法审查维度 {payload.dimension!r}')

    with session_scope() as session:
        row = session.get(Rule, rule_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'规则 {rule_id} 不存在', http_status=404)

        if payload.template_clause_id is not None:
            if session.get(TemplateClause, payload.template_clause_id) is None:
                raise ApiError(ErrorCode.NOT_FOUND,
                               f'示范条款 {payload.template_clause_id} 不存在', http_status=404)
            row.template_clause_id = payload.template_clause_id

        for field in ('name', 'contract_type', 'dimension', 'trigger_condition',
                      'risk_level', 'legal_basis', 'enabled', 'priority'):
            value = getattr(payload, field)
            if value is not None:
                setattr(row, field, value)

        session.flush()
        result = _rule_out(row)

    reset_rule_cache()
    logger.info(f'[接入] 更新规则 {rule_id}（操作人 {current_user["username"]}）')
    return ok(result, message='规则已更新')


@router.delete('/rules/{rule_id}', summary='删除规则')
def delete_rule(rule_id: int, current_user: Dict[str, Any] = Depends(_CAN_WRITE)
                ) -> Dict[str, Any]:
    """
    删除规则

    `risk_item.matched_rule_id` 是 `SET NULL`，删完后历史风险项的依据列会变空 ——
    所以响应里把影响面告诉调用方，前端可以做二次确认。
    """
    with session_scope() as session:
        row = session.get(Rule, rule_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'规则 {rule_id} 不存在', http_status=404)

        code = row.code
        affected = session.scalar(
            select(func.count()).select_from(RiskItem)
            .where(RiskItem.matched_rule_id == rule_id)
        ) or 0
        session.delete(row)

    reset_rule_cache()
    logger.info(f'[接入] 删除规则 {code}（影响 {affected} 条历史风险项的依据）')
    return ok({'deleted': rule_id, 'code': code, 'affected_risk_items': affected},
              message=f'规则已删除（{affected} 条历史风险项的规则依据将被置空）')


# ============================================================
# 示范条款
# ============================================================
@router.get('/templates', summary='示范条款列表')
def list_templates(query: ListQuery = Depends(),
                   current_user: Dict[str, Any] = Depends(_CAN_READ)) -> Dict[str, Any]:
    """示范条款列表（推荐引擎的召回来源）"""
    conditions = []
    if query.contract_type:
        conditions.append(TemplateClause.contract_type == query.contract_type)
    if query.keyword:
        like = f'%{query.keyword.strip()}%'
        conditions.append(TemplateClause.title.like(like) | TemplateClause.content.like(like))

    with session_scope() as session:
        count_stmt = select(func.count()).select_from(TemplateClause)
        rows_stmt = select(TemplateClause).order_by(
            TemplateClause.contract_type, TemplateClause.clause_type, TemplateClause.id
        )
        if conditions:
            count_stmt = count_stmt.where(*conditions)
            rows_stmt = rows_stmt.where(*conditions)

        total = session.scalar(count_stmt) or 0
        rows = session.scalars(
            rows_stmt.offset((query.page - 1) * query.page_size).limit(query.page_size)
        ).all()

        ref_rows = session.execute(
            select(Rule.template_clause_id, func.count())
            .where(Rule.template_clause_id.is_not(None))
            .group_by(Rule.template_clause_id)
        ).all()
        ref_map = {tid: count for tid, count in ref_rows}
        items = [_template_out(row, ref_map.get(row.id, 0)) for row in rows]

    return paged(items, total=total, page=query.page, page_size=query.page_size)


@router.post('/templates', status_code=201, summary='新增示范条款')
def create_template(payload: TemplateCreateRequest,
                    current_user: Dict[str, Any] = Depends(_CAN_WRITE)) -> Dict[str, Any]:
    """新增示范��款"""
    if payload.clause_type not in ClauseType.ALL:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f'非法条款类型 {payload.clause_type!r}，可选 {list(ClauseType.ALL)}',
        )

    with session_scope() as session:
        duplicated = session.scalars(
            select(TemplateClause).where(
                TemplateClause.contract_type == payload.contract_type,
                TemplateClause.clause_type == payload.clause_type,
                TemplateClause.title == payload.title,
            )
        ).first()
        if duplicated is not None:
            raise ApiError(
                ErrorCode.CONFLICT,
                f'同合同类型 + 条款类型下已存在同名示范条款（id={duplicated.id}）',
                http_status=409,
            )

        row = TemplateClause(
            contract_type=payload.contract_type, clause_type=payload.clause_type,
            title=payload.title, content=payload.content, enabled=payload.enabled,
        )
        session.add(row)
        session.flush()
        result = _template_out(row)

    logger.info(f'[接入] 新增示范条款 {payload.title}（操作人 {current_user["username"]}）')
    return ok(result, message='示范条款已创建')


@router.put('/templates/{template_id}', summary='编辑示范条款')
def update_template(template_id: int, payload: TemplateUpdateRequest,
                    current_user: Dict[str, Any] = Depends(_CAN_WRITE)) -> Dict[str, Any]:
    """编辑示范条款"""
    if payload.clause_type is not None and payload.clause_type not in ClauseType.ALL:
        raise ApiError(ErrorCode.VALIDATION_FAILED,
                       f'非法条款类型 {payload.clause_type!r}')

    with session_scope() as session:
        row = session.get(TemplateClause, template_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'示范条款 {template_id} 不存在', http_status=404)

        for field in ('clause_type', 'title', 'content', 'contract_type', 'enabled'):
            value = getattr(payload, field)
            if value is not None:
                setattr(row, field, value)

        session.flush()
        result = _template_out(row)

    logger.info(f'[接入] 更新示范条款 {template_id}（操作人 {current_user["username"]}）')
    return ok(result, message='示范条款已更新')


@router.delete('/templates/{template_id}', summary='删除示范条款')
def delete_template(template_id: int, current_user: Dict[str, Any] = Depends(_CAN_WRITE)
                    ) -> Dict[str, Any]:
    """
    删除示范条款

    `rule.template_clause_id` 是 SET NULL：如果这条模板被规则绑定着，
    删掉后那些规则就失去了"推荐模板"，推荐引擎会退化为按合同类型 + 条款类型召回。
    所以响应里把引用它的规则数一并返回。
    """
    with session_scope() as session:
        row = session.get(TemplateClause, template_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'示范条款 {template_id} 不存在', http_status=404)

        title = row.title
        affected = session.scalar(
            select(func.count()).select_from(Rule).where(Rule.template_clause_id == template_id)
        ) or 0
        session.delete(row)

    reset_rule_cache()  # 规则与模板的关联变了，缓存里的 template_clause_id 可能已失效
    logger.info(f'[接入] 删除示范条款 {title}（影响 {affected} 条规则的推荐模板绑定）')
    return ok({'deleted': template_id, 'title': title, 'affected_rules': affected},
              message=f'示范条款已删除（{affected} 条规则将失去绑定的推荐模板）')


# ============================================================
# 元数据字典（前端下拉框用，省得前端硬编码枚举）
# ============================================================
@router.get('/rules/meta/options', summary='规则可选项字典')
def rule_options(current_user: Dict[str, Any] = Depends(_CAN_READ)) -> Dict[str, Any]:
    """给规则编辑页提供枚举与算子清单，避免前后端各写一份枚举"""
    return ok({
        'risk_levels': list(RiskLevel.ALL),
        'dimensions': list(RiskDimension.ALL),
        'clause_types': list(ClauseType.ALL),
        'operators': sorted(SUPPORTED_OPERATORS),
        'logic_keys': sorted(LOGIC_KEYS),
        'envelope_keys': sorted(ENVELOPE_KEYS),
        'default_contract_type': '*',
    })


if __name__ == '__main__':
    # 冒烟自测：路由注册清单 + 可装配性。
    # 规则/模板的完整 CRUD 流程由 scripts/run_e2e.py 覆盖（第 12/13 步），
    # 本文件只做轻量冒烟，避免双份维护。
    import logging
    import tempfile
    from pathlib import Path

    logging.basicConfig(level=logging.ERROR, format='%(levelname)s - %(message)s')
    passed = 0

    paths = {route.path for route in router.routes}
    spec_required = {
        '/api/rules', '/api/rules/{rule_id}',
        '/api/templates', '/api/templates/{template_id}',
    }
    missing = spec_required - paths
    assert not missing, f'SPEC 7.3 要求的接口未注册：{sorted(missing)}'
    assert '/api/rules/meta/options' in paths, '规则可选项字典接口缺失'
    passed += 1
    print(f'  ✅ SPEC 7.3 规则/模板接口全部注册（共 {len(paths)} 个）')

    # GET/POST 在同一个 path 上注册为不同方法 —— 校验方法集完整
    methods_by_path: dict = {}
    for route in router.routes:
        methods_by_path.setdefault(route.path, set()).update(route.methods or set())
    assert methods_by_path['/api/rules'] >= {'GET', 'POST'}, '规则列表应支持 GET+POST'
    assert methods_by_path['/api/rules/{rule_id}'] >= {'PUT', 'DELETE'}, '规则应支持 PUT+DELETE'
    assert methods_by_path['/api/templates'] >= {'GET', 'POST'}
    assert methods_by_path['/api/templates/{template_id}'] >= {'PUT', 'DELETE'}
    passed += 1
    print('  ✅ 规则/模板 CRUD 的 HTTP 方法集完整（GET/POST/PUT/DELETE）')

    # 应用可装配 + 未登录 401
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from db.session import init_db, init_engine, reset_engine
    from web.errors import ErrorCode, register_exception_handlers

    _dir = Path(tempfile.mkdtemp(prefix='rule_svc_test_'))
    reset_engine()
    init_engine(f'sqlite:///{_dir.as_posix()}/t.db')
    init_db()

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    client = TestClient(app)
    resp = client.get('/api/rules')
    assert resp.status_code == 401 and resp.json()['code'] == ErrorCode.UNAUTHORIZED
    resp = client.post('/api/rules', json={})
    assert resp.status_code == 401, '未登录建规则应 401'
    passed += 1
    print('  ✅ 应用可装配；未登录访问一律 401 + 3001')

    reset_engine()
    print(f'rule_service 自测全部通过 ✅ 共 {passed} 个用例（CRUD 全链路见 scripts/run_e2e.py）')

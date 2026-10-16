'''db/seed.py'''

"""
规则与示范条款种子数据

严格对应 SPEC 第 4.3.3 节「风险判定硬规则（初版库）」的 R-01 ~ R-08，
以及第 4.4 节「条款推荐引擎」需要的标准示范条款库。

trigger_condition 的结构:
    采用「信封 + 条件树」两段式：

    {
      "scope":     "clause" | "document",   # 可选，默认 clause
      "risk_type": "missing" | "unbalanced" | ...,  # 可选，默认 high_risk_exp
      "when":      { ...条件树... }          # 必填
    }

    - `scope = clause`    → 规则逐条条款求值，命中即产出一条风险项
    - `scope = document`  → 规则对整份合同只求值一次（用于 R-01 这类跨条款/看元数据的规则）

    兼容 SPEC 9.2 的简写形式：若根节点直接是 `and` / `or` / `not`，
    则整棵树即条件树，scope 默认 clause、risk_type 默认 high_risk_exp。

支持算子（SPEC 9.2）:
    eq / contains / contains_any / regex / not_contains / exists / missing / gt / lt
    —— 另外补充了 **not_regex**：R-05 / R-06 这类「有该条款但缺某个限定」的判定
    用 not_contains 无法表达（会把任何含"年"字的文本都判为已约定期限），必须用否定正则。

幂等性:
    示范条款按 (contract_type, clause_type, title) 去重，规则按 code 去重，
    重复执行只会更新内容，不会产生重复行。
"""

import json
import logging
from typing import Any, Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.constants import ClauseType, RiskDimension, RiskLevel, RiskType, UserRole
from db.models import AppUser, Rule, TemplateClause
from db.session import session_scope
# 密码哈希在 utils（放在 web 层会让数据层反向依赖接入层）
from utils.security_utils import hash_password

logger = logging.getLogger(__name__)


# ============================================================
# 演示账号（SPEC 12：RBAC 三角色）
# ============================================================
# 仅用于演示与联调。**生产环境必须改掉这些弱密码**（README/.env.example 里都标注了）。
DEMO_USERS: List[Dict[str, Any]] = [
    {
        'username': 'legal',
        'password': 'legal123',
        'role': UserRole.LEGAL,
        'display_name': '法务审查人',
    },
    {
        'username': 'business',
        'password': 'business123',
        'role': UserRole.BUSINESS,
        'display_name': '业务经办人',
    },
    {
        'username': 'admin',
        'password': 'admin123',
        'role': UserRole.ADMIN,
        'display_name': '系统管理员',
    },
]


# ============================================================
# 标准示范条款库（SPEC 4.4 的召回来源）
# ============================================================
TEMPLATE_CLAUSES: List[Dict[str, Any]] = [
    {
        'contract_type': '*',
        'clause_type': ClauseType.PAYMENT,
        'title': '付款与验收前置条款（示范）',
        'content': (
            '甲方应在收到乙方交付的标的物并完成验收合格后 15 个工作日内支付相应款项；'
            '未经验收或验收不合格的，甲方有权拒付相应款项。'
            '付款按里程碑分期进行：本合同生效后支付合同总金额的 30%，'
            '全部标的物验收合格后支付 60%，质保期满且无质量异议后支付剩余 10%。'
        ),
    },
    {
        'contract_type': '*',
        'clause_type': ClauseType.BREACH,
        'title': '对等违约责任条款（示范）',
        'content': (
            '任何一方违反本合同约定的，应向守约方支付违约金，'
            '违约金总额累计不超过合同总金额的 20%；'
            '因违约给对方造成损失的，赔偿金额以合同总金额为限，双方承担对等的违约责任。'
        ),
    },
    {
        'contract_type': '*',
        'clause_type': ClauseType.IP,
        'title': '知识产权归属条款（示范）',
        'content': (
            '乙方为履行本合同专门开发的软件、源代码及相关文档的知识产权归甲方所有；'
            '乙方在本项目中使用的自有底层技术、通用组件及第三方组件的知识产权仍归乙方或原权利人所有，'
            '乙方在此授予甲方永久、免费、不可撤销、可转授的使用许可。'
        ),
    },
    {
        'contract_type': '*',
        'clause_type': ClauseType.CONFIDENTIALITY,
        'title': '保密期限条款（示范）',
        'content': (
            '双方的保密义务自本合同生效之日起至本合同终止或解除后 5 年内持续有效，'
            '不因本合同的终止、解除或履行完毕而免除。'
        ),
    },
    {
        'contract_type': '*',
        'clause_type': ClauseType.FORCE_MAJEURE,
        'title': '不可抗力通知时效条款（示范）',
        'content': (
            '遭受不可抗力的一方应在不可抗力事件发生后 15 日内书面通知对方，'
            '并在 30 日内提供有权机关出具的证明文件；'
            '未按前述期限通知并举证的，不得据此主张免除或减轻责任。'
        ),
    },
    {
        'contract_type': '*',
        'clause_type': ClauseType.JURISDICTION,
        'title': '争议管辖条款（示范）',
        'content': (
            '因本合同引起的或与本合同有关的任何争议，双方应首先友好协商解决；'
            '协商不成的，任何一方均可向甲方所在地有管辖权的人民法院提起诉讼。'
        ),
    },
    {
        'contract_type': '*',
        'clause_type': ClauseType.SUBJECT,
        'title': '签约主体资质条款（示范）',
        'content': (
            '双方确认各自具备签订及履行本合同所需的主体资格与相应资质，'
            '签约时应相互提供营业执照副本、统一社会信用代码等证明文件，'
            '并保证所提供资料真实、合法、有效。'
        ),
    },
]


# ============================================================
# 审查规则（SPEC 4.3.3 的 R-01 ~ R-08）
# ============================================================
RULES: List[Dict[str, Any]] = [
    {
        'code': 'R-01',
        'name': '主体信息缺失或异常',
        'contract_type': '*',
        'dimension': RiskDimension.SUBJECT,
        'risk_level': RiskLevel.HIGH,
        'template_clause_type': ClauseType.SUBJECT,
        'legal_basis': '《民法典》第143条、第502条；《市场主体登记管理条例》',
        'trigger_condition': {
            'scope': 'document',
            'risk_type': RiskType.MISSING,
            'when': {
                'or': [
                    {'field': 'party_b', 'op': 'missing'},
                    {'field': 'credit_code', 'op': 'missing'},
                    {'field': 'party_b', 'op': 'contains_any',
                     'value': ['经营异常', '失信', '已吊销', '注销']},
                ]
            },
        },
        'priority': 10,
    },
    {
        'code': 'R-02',
        'name': '违约责任严重不对等',
        'contract_type': '*',
        'dimension': RiskDimension.BREACH,
        'risk_level': RiskLevel.HIGH,
        'template_clause_type': ClauseType.BREACH,
        'legal_basis': '《民法典》第584条、第585条',
        'trigger_condition': {
            'scope': 'clause',
            'risk_type': RiskType.UNBALANCED,
            'when': {
                'and': [
                    {'field': 'clause_type', 'op': 'eq', 'value': ClauseType.BREACH},
                    {'field': 'content', 'op': 'contains_any',
                     'value': ['无上限', '无限责任', '承担全部损失', '全额赔偿',
                               '承担一切责任', '不设赔偿上限', '不受上限限制']},
                ]
            },
        },
        'priority': 20,
    },
    {
        'code': 'R-03',
        'name': '管辖约定不利于己方',
        'contract_type': '*',
        'dimension': RiskDimension.JURISDICTION,
        'risk_level': RiskLevel.HIGH,
        'template_clause_type': ClauseType.JURISDICTION,
        'legal_basis': '《民事诉讼法》第24条、第35条',
        'trigger_condition': {
            'scope': 'clause',
            'risk_type': RiskType.UNBALANCED,
            'when': {
                'and': [
                    {'field': 'clause_type', 'op': 'eq', 'value': ClauseType.JURISDICTION},
                    {'field': 'content', 'op': 'contains_any',
                     'value': ['乙方所在地人民法院', '乙方所在地法院',
                               '供应商所在地人民法院', '对方所在地人民法院',
                               '对方所在地法院', '供方所在地']},
                ]
            },
        },
        'priority': 30,
    },
    {
        'code': 'R-04',
        'name': '付款无验收前置条件',
        'contract_type': '*',
        'dimension': RiskDimension.PAYMENT,
        'risk_level': RiskLevel.HIGH,
        'template_clause_type': ClauseType.PAYMENT,
        'legal_basis': '《民法典》第626条、第621条',
        # 条件树与 SPEC 9.2 的示例保持一致
        'trigger_condition': {
            'and': [
                {'field': 'clause_type', 'op': 'eq', 'value': ClauseType.PAYMENT},
                {
                    'or': [
                        {'field': 'content', 'op': 'contains_any',
                         'value': ['到货即付', '货到付款全款', '货到即付']},
                        # SPEC 9.2 给的是「验收.*未|无.*验收」，但漏了语序相反的写法
                        # （如「未设验收条件」），这里补上反向分支
                        {'field': 'content', 'op': 'regex',
                         'value': '验收.*未|未.*验收|无.*验收|验收.*无'},
                    ]
                },
            ]
        },
        'priority': 40,
    },
    {
        'code': 'R-05',
        'name': '保密义务缺少期限限定',
        'contract_type': '*',
        'dimension': RiskDimension.CONFIDENTIALITY,
        'risk_level': RiskLevel.MID,
        'template_clause_type': ClauseType.CONFIDENTIALITY,
        'legal_basis': '《反不正当竞争法》第9条；《民法典》第501条',
        'trigger_condition': {
            'scope': 'clause',
            'risk_type': RiskType.MISSING,
            'when': {
                'and': [
                    {'field': 'clause_type', 'op': 'eq', 'value': ClauseType.CONFIDENTIALITY},
                    # 兼容两种写法：「保密期限为 5 年」与「保密义务持续 5 年」
                    {'field': 'content', 'op': 'not_regex',
                     'value': '(保密期(限)?|保密义务|保密责任)[^。；;]{0,30}?'
                              '(\\d+|[一二三四五六七八九十]+)\\s*年'},
                ]
            },
        },
        'priority': 50,
    },
    {
        'code': 'R-06',
        'name': '不可抗力缺少通知时效',
        'contract_type': '*',
        'dimension': RiskDimension.BREACH,
        'risk_level': RiskLevel.MID,
        'template_clause_type': ClauseType.FORCE_MAJEURE,
        'legal_basis': '《民法典》第590条',
        'trigger_condition': {
            'scope': 'clause',
            'risk_type': RiskType.MISSING,
            'when': {
                'and': [
                    {'field': 'clause_type', 'op': 'eq', 'value': ClauseType.FORCE_MAJEURE},
                    {'field': 'content', 'op': 'not_regex',
                     'value': '(\\d+|[一二三四五六七八九十]+)\\s*(日|天|小时|个工作日)'},
                ]
            },
        },
        'priority': 60,
    },
    {
        'code': 'R-07',
        'name': '违约金比例超出合理范围',
        'contract_type': '*',
        'dimension': RiskDimension.BREACH,
        'risk_level': RiskLevel.MID,
        'template_clause_type': ClauseType.BREACH,
        'legal_basis': '《民法典》第585条（违约金过分高于损失可请求调减）',
        'trigger_condition': {
            'scope': 'clause',
            'risk_type': RiskType.AMOUNT,
            'when': {
                'and': [
                    {'field': 'clause_type', 'op': 'eq', 'value': ClauseType.BREACH},
                    # 抓 21% 及以上；两种语序都要覆盖：
                    #   「违约金为合同总额的 30%」（比例在后）
                    #   「按合同总额的 30% 支付违约金」（比例在前）
                    {'field': 'content', 'op': 'regex',
                     'value': '违约金[^。；;]{0,40}?(2[1-9]|[3-9]\\d|[1-9]\\d{2,})\\s*%'
                              '|(2[1-9]|[3-9]\\d|[1-9]\\d{2,})\\s*%[^。；;]{0,40}?违约金'},
                ]
            },
        },
        'priority': 70,
    },
    {
        'code': 'R-08',
        'name': '知识产权归属不利',
        'contract_type': '*',
        'dimension': RiskDimension.IP,
        'risk_level': RiskLevel.HIGH,
        'template_clause_type': ClauseType.IP,
        'legal_basis': '《民法典》第843条、第861条；《计算机软件保护条例》',
        'trigger_condition': {
            'scope': 'clause',
            'risk_type': RiskType.HIGH_RISK_EXP,
            'when': {
                'and': [
                    {'field': 'clause_type', 'op': 'eq', 'value': ClauseType.IP},
                    {'field': 'content', 'op': 'contains_any',
                     'value': ['知识产权归乙方所有', '知识产权归供应商所有',
                               '知识产权归供方所有', '知识产权归对方所有',
                               '归乙方独占所有']},
                ]
            },
        },
        'priority': 80,
    },
]


def seed_templates(session: Session) -> Tuple[int, int]:
    """
    写入 / 更新标准示范条款库（幂等）
    :param session: 数据库会话
    :return: (新增数, 更新数)
    """
    created, updated = 0, 0

    for item in TEMPLATE_CLAUSES:
        record = session.scalars(
            select(TemplateClause).where(
                TemplateClause.contract_type == item['contract_type'],
                TemplateClause.clause_type == item['clause_type'],
                TemplateClause.title == item['title'],
            )
        ).first()

        if record is None:
            session.add(TemplateClause(**item, enabled=True))
            created += 1
        else:
            changed = False
            if record.content != item['content']:
                record.content = item['content']
                changed = True
            if not record.enabled:
                record.enabled = True
                changed = True
            if changed:
                updated += 1

    session.flush()
    return created, updated


def seed_rules(session: Session) -> Tuple[int, int]:
    """
    写入 / 更新审查规则（幂等，按 code 去重）
    功能：同时把规则关联到对应的示范条款上（按 clause_type 找模板）
    :param session: 数据库会话
    :return: (新增数, 更新数)
    """
    created, updated = 0, 0

    # 先把示范条款按 clause_type 建索引，供规则关联
    template_id_by_type = {
        template.clause_type: template.id
        for template in session.scalars(select(TemplateClause)).all()
    }

    for item in RULES:
        payload = dict(item)
        template_clause_id = template_id_by_type.get(payload.pop('template_clause_type', None))

        record = session.scalars(
            select(Rule).where(Rule.code == payload['code'])
        ).first()

        if record is None:
            session.add(Rule(
                **payload,
                template_clause_id=template_clause_id,
                enabled=True,
            ))
            created += 1
            continue

        # 已存在 → 逐字段比对，有变化才更新（保留管理员可能的开关改动？不 —— 种子以代码为准）
        changed = False
        for field_name, value in payload.items():
            if getattr(record, field_name) != value:
                setattr(record, field_name, value)
                changed = True
        if record.template_clause_id != template_clause_id:
            record.template_clause_id = template_clause_id
            changed = True
        if changed:
            updated += 1

    session.flush()
    return created, updated


def seed_users(session: Session) -> int:
    """
    写入演示账号（幂等）
    功能：按 username 去重。**已存在的账号一律不动** —— 包括不改密码、
        不覆盖角色，否则管理员改过的密码会被种子脚本重置回去。
    :param session: 数据库会话
    :return: 新增数
    """
    created = 0

    for item in DEMO_USERS:
        record = session.scalars(
            select(AppUser).where(AppUser.username == item['username'])
        ).first()
        if record is not None:
            continue
        session.add(AppUser(
            username=item['username'],
            password_hash=hash_password(item['password']),
            role=item['role'],
            display_name=item['display_name'],
        ))
        created += 1

    session.flush()
    return created


def seed_all() -> Dict[str, int]:
    """
    写入全部种子数据（幂等，可重复执行）
    :return: 各类数据的写入统计
    """
    with session_scope() as session:
        template_created, template_updated = seed_templates(session)
        rule_created, rule_updated = seed_rules(session)
        user_created = seed_users(session)

    result = {
        'template_created': template_created,
        'template_updated': template_updated,
        'rule_created': rule_created,
        'rule_updated': rule_updated,
        'user_created': user_created,
    }
    logger.info(f'种子数据写入完成：{result}')
    return result


if __name__ == '__main__':
    # 自测：临时库上验证幂等与关联关系
    import tempfile
    from pathlib import Path

    from sqlalchemy import inspect

    from db.session import init_db, init_engine, reset_engine

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    work_dir = Path(tempfile.mkdtemp(prefix='db_seed_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/seed_test.db')
    init_db()
    passed = 0

    # ---------- 一、首次写入 ----------
    first = seed_all()
    assert first['template_created'] == len(TEMPLATE_CLAUSES), \
        f"应写入 {len(TEMPLATE_CLAUSES)} 条示范条款，实际 {first['template_created']}"
    assert first['rule_created'] == len(RULES), \
        f"应写入 {len(RULES)} 条规则，实际 {first['rule_created']}"
    assert first['user_created'] == len(DEMO_USERS), \
        f"应写入 {len(DEMO_USERS)} 个演示账号，实际 {first['user_created']}"
    assert first['template_updated'] == 0 and first['rule_updated'] == 0
    passed += 1
    logging.getLogger().info(
        f'  ✅ 首次写入：{len(TEMPLATE_CLAUSES)} 条模板 + {len(RULES)} 条规则 '
        f'+ {len(DEMO_USERS)} 个演示账号'
    )

    # ---------- 二、重复执行必须幂等 ----------
    second = seed_all()
    assert second['template_created'] == 0 and second['rule_created'] == 0, \
        '重复执行不应新增任何数据'
    assert second['user_created'] == 0, '重复执行不应新增账号'
    assert second['template_updated'] == 0 and second['rule_updated'] == 0, \
        '内容未变时不应产生更新'
    passed += 1
    logging.getLogger().info('  ✅ 重复执行幂等，无新增无更新')

    # ---------- 三、规则编码与等级完全符合 SPEC ----------
    from db.session import get_engine, session_scope

    with session_scope() as session:
        rules = session.scalars(select(Rule).order_by(Rule.code)).all()

        assert [r.code for r in rules] == [f'R-0{i}' for i in range(1, 9)], \
            f'规则编码应为 R-01~R-08，实际 {[r.code for r in rules]}'

        level_by_code = {r.code: r.risk_level for r in rules}
        # SPEC 4.3.3：R-01/02/03/04/08 高风险，R-05/06/07 中风险
        for code in ('R-01', 'R-02', 'R-03', 'R-04', 'R-08'):
            assert level_by_code[code] == 'high', f'{code} 应为高风险，实际 {level_by_code[code]}'
        for code in ('R-05', 'R-06', 'R-07'):
            assert level_by_code[code] == 'mid', f'{code} 应为中风险，实际 {level_by_code[code]}'

        # 每条规则都必须带合规依据（SPEC 4.3.3 要求输出 legal_basis）
        for rule in rules:
            assert rule.legal_basis, f'{rule.code} 缺少合规依据'
            assert rule.trigger_condition, f'{rule.code} 缺少触发条件'

        # R-04 的条件树应与 SPEC 9.2 的示例一致
        r04 = next(r for r in rules if r.code == 'R-04')
        assert 'and' in r04.trigger_condition, 'R-04 应使用 SPEC 9.2 的简写形式（根即条件树）'
        assert r04.trigger_condition['and'][0]['field'] == 'clause_type'
    passed += 1
    logging.getLogger().info('  ✅ R-01~R-08 编码、等级、合规依据全部符合 SPEC 4.3.3')

    # ---------- 四、规则关联到示范条款 ----------
    with session_scope() as session:
        rules = session.scalars(select(Rule)).all()
        for rule in rules:
            assert rule.template_clause_id is not None, \
                f'{rule.code} 未关联到示范条款（推荐引擎需要它）'
            assert rule.template_clause is not None, f'{rule.code} 的模板关联查询失败'
        passed += 1
        logging.getLogger().info('  ✅ 8 条规则均已关联示范条款')

    # ---------- 五、模板召回按 (contract_type, clause_type) 可查 ----------
    with session_scope() as session:
        payment_templates = session.scalars(
            select(TemplateClause).where(TemplateClause.clause_type == ClauseType.PAYMENT)
        ).all()
        assert len(payment_templates) >= 1, '付款类示范条款应可召回'
        assert all(t.enabled for t in payment_templates), '示范条款应默认启用'
    passed += 1
    logging.getLogger().info('  ✅ 示范条款可按合同类型/条款类型召回')

    # ---------- 六、演示账号 ----------
    from db.constants import UserRole
    from utils.security_utils import verify_password

    with session_scope() as session:
        users = {u.username: u for u in session.scalars(select(AppUser)).all()}

        assert set(users.keys()) == {u['username'] for u in DEMO_USERS}, \
            f'账号集合不一致：{sorted(users.keys())}'
        assert len(users) == 3, 'SPEC 12 的三个角色各一个演示账号'

        roles = {u.username: u.role for u in users.values()}
        assert roles == {'legal': UserRole.LEGAL, 'business': UserRole.BUSINESS,
                         'admin': UserRole.ADMIN}, f'角色分配不正确：{roles}'

        # 密码必须能校验通过，且库里**绝不能是明文**
        for item in DEMO_USERS:
            user = users[item['username']]
            assert user.password_hash and user.password_hash != item['password'], \
                f"{item['username']} 的密码被明文存储了"
            assert user.password_hash.startswith('$2'), '应为 bcrypt 哈希'
            assert verify_password(item['password'], user.password_hash) is True, \
                f"{item['username']} 的种子密码校验失败"
            assert verify_password('wrong-password', user.password_hash) is False
            assert user.display_name, '应有展示名'
    passed += 1
    logging.getLogger().info('  ✅ 3 个演示账号角色正确，密码以 bcrypt 存储且可校验')

    # 关键：重复执行种子**不能重置已有账号的密码**
    with session_scope() as session:
        original_hash = session.scalars(
            select(AppUser).where(AppUser.username == 'admin')
        ).first().password_hash

    seed_all()

    with session_scope() as session:
        after_hash = session.scalars(
            select(AppUser).where(AppUser.username == 'admin')
        ).first().password_hash
        assert after_hash == original_hash, \
            '重复执行种子脚本绝不能重置已有账号的密码（管理员改过的密码会被冲掉）'
    passed += 1
    logging.getLogger().info('  ✅ 重复执行不重置已有账号密码')

    reset_engine()
    logging.getLogger().info(f'db.seed 自测全部通过 ✅ 共 {passed} 个用例')

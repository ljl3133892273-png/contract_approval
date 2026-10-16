'''db/constants.py'''

"""
数据库字段的取值常量

为什么不用 Enum 类型:
    SPEC 6.2 把这些字段定义为 VARCHAR（如 review_status VARCHAR(16)），
    库里存的是可读字符串，便于直接用 SQL 排查与跨系统对接。
    这里用「字符串常量类」而不是 Python Enum，是为了让它能直接和字符串比较、
    直接写进数据库，不用到处 .value。

对应 SPEC 5.1（主状态机）、5.3（回写状态机）、6.2（字段取值）。
"""


class ReviewStatus:
    """审查主状态（SPEC 5.1）"""

    PENDING = 'pending'  # 待处理
    PARSING = 'parsing'  # 解析中
    REVIEWING = 'reviewing'  # 审查中
    BLOCKED = 'blocked'  # 解析/审查受阻
    COMPLETED = 'completed'  # 审查完成（终态）

    ALL = (PENDING, PARSING, REVIEWING, BLOCKED, COMPLETED)
    # 终态：不允许再流转
    FINAL = (COMPLETED,)


class WritebackStatus:
    """回写状态（SPEC 5.3）"""

    NOT_WRITTEN = 'not_written'
    WRITING = 'writing'
    SUCCESS = 'success'
    FAILED = 'failed'

    ALL = (NOT_WRITTEN, WRITING, SUCCESS, FAILED)
    FINAL = (SUCCESS,)


class TaskEvent:
    """状态机事件（SPEC 5.2）"""

    CREATE = 'CREATE'
    PARSE_START = 'PARSE_START'
    PARSE_OK = 'PARSE_OK'
    PARSE_FAIL = 'PARSE_FAIL'
    REVIEW_OK = 'REVIEW_OK'
    REVIEW_FAIL = 'REVIEW_FAIL'
    RETRY = 'RETRY'
    WRITE_START = 'WRITE_START'
    WRITE_OK = 'WRITE_OK'
    WRITE_FAIL = 'WRITE_FAIL'
    RETRY_WRITE = 'RETRY_WRITE'

    ALL = (CREATE, PARSE_START, PARSE_OK, PARSE_FAIL, REVIEW_OK,
           REVIEW_FAIL, RETRY, WRITE_START, WRITE_OK, WRITE_FAIL, RETRY_WRITE)


class RiskLevel:
    """风险等级（SPEC 4.3.2）"""

    HIGH = 'high'
    MID = 'mid'
    LOW = 'low'
    NONE = 'none'  # 尚未审查出结果

    ALL = (HIGH, MID, LOW, NONE)
    # 严重度排序，用于「取最高」的综合风险计算
    SEVERITY = {HIGH: 3, MID: 2, LOW: 1, NONE: 0}


class RiskSource:
    """风险项来源"""

    RULE = 'rule'
    LLM = 'llm'
    MANUAL = 'manual'

    ALL = (RULE, LLM, MANUAL)


class AdoptStatus:
    """法务对建议的采纳状态（SPEC 4.3）"""

    PENDING = 'pending'
    ADOPTED = 'adopted'
    ADJUSTED = 'adjusted'
    REJECTED = 'rejected'

    ALL = (PENDING, ADOPTED, ADJUSTED, REJECTED)


class SourceType:
    """任务接入来源（SPEC 4.1.1）"""

    UPLOAD = 'upload'
    APPROVE_PUSH = 'approve_push'
    APPROVE_PULL = 'approve_pull'

    ALL = (UPLOAD, APPROVE_PUSH, APPROVE_PULL)
    # 审批来源：附件需由 Adapter 下载到本地
    APPROVE_SOURCES = (APPROVE_PUSH, APPROVE_PULL)


class FileType:
    """文件类别（node_detect_type 的权威判定结果）"""

    DOCX = 'docx'
    PDF = 'pdf'
    IMAGE = 'image'

    ALL = (DOCX, PDF, IMAGE)


class ClauseType:
    """条款类型（SPEC 6.2 clause.clause_type）"""

    SUBJECT = 'subject'  # 标的物
    PAYMENT = 'payment'  # 付款
    ACCEPTANCE = 'acceptance'  # 验收
    BREACH = 'breach'  # 违约责任
    CONFIDENTIALITY = 'confidentiality'  # 保密义务
    DATA_SECURITY = 'data_security'  # 数据安全
    JURISDICTION = 'jurisdiction'  # 争议管辖
    IP = 'ip'  # 知识产权
    FORCE_MAJEURE = 'force_majeure'  # 不可抗力
    TERM = 'term'  # 期限
    OTHER = 'other'  # 兜底

    ALL = (SUBJECT, PAYMENT, ACCEPTANCE, BREACH, CONFIDENTIALITY, DATA_SECURITY,
           JURISDICTION, IP, FORCE_MAJEURE, TERM, OTHER)


class RiskDimension:
    """审查维度（SPEC 4.3.1）"""

    SUBJECT = 'subject'  # 主体资质
    PAYMENT = 'payment'  # 金额支付
    BREACH = 'breach'  # 违约责任
    IP = 'ip'  # 知识产权
    DATA_SECURITY = 'data_security'  # 数据安全
    CONFIDENTIALITY = 'confidentiality'  # 保密
    JURISDICTION = 'jurisdiction'  # 争议管辖
    ACCEPTANCE = 'acceptance'  # 验收标准

    ALL = (SUBJECT, PAYMENT, BREACH, IP, DATA_SECURITY,
           CONFIDENTIALITY, JURISDICTION, ACCEPTANCE)


class RiskType:
    """风险类型（SPEC 6.2 risk_item.risk_type）"""

    MISSING = 'missing'  # 必备条款缺失
    UNBALANCED = 'unbalanced'  # 权责不对等
    HIGH_RISK_EXP = 'high_risk_exp'  # 高风险表述
    AMOUNT = 'amount'  # 金额相关
    TIMING = 'timing'  # 时限相关

    ALL = (MISSING, UNBALANCED, HIGH_RISK_EXP, AMOUNT, TIMING)


class UserRole:
    """用户角色（SPEC 3）"""

    LEGAL = 'legal'  # 法务审查人
    BUSINESS = 'business'  # 业务经办人
    ADMIN = 'admin'  # 系统管理员

    ALL = (LEGAL, BUSINESS, ADMIN)


class ReportFormat:
    """报告导出格式（SPEC 10.2）"""

    PDF = 'pdf'
    MD = 'md'

    ALL = (PDF, MD)


class TaskStatusText:
    """
    节点级任务状态（内存态进度，见 utils/task_utils.py）
    与 ReviewStatus 区分：这个是"图跑到哪一步"，那个是"业务主状态"。
    """

    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'

    ALL = (PENDING, PROCESSING, COMPLETED, FAILED)


if __name__ == '__main__':
    # 轻量自测：常量不重复、严重度排序正确
    all_groups = {
        'ReviewStatus': ReviewStatus.ALL,
        'WritebackStatus': WritebackStatus.ALL,
        'RiskLevel': RiskLevel.ALL,
        'RiskSource': RiskSource.ALL,
        'AdoptStatus': AdoptStatus.ALL,
        'SourceType': SourceType.ALL,
        'FileType': FileType.ALL,
        'ClauseType': ClauseType.ALL,
        'RiskDimension': RiskDimension.ALL,
        'RiskType': RiskType.ALL,
        'UserRole': UserRole.ALL,
        'ReportFormat': ReportFormat.ALL,
    }
    for group_name, values in all_groups.items():
        assert len(values) == len(set(values)), f'{group_name} 存在重复取值'

    # 严重度排序：high > mid > low > none
    assert RiskLevel.SEVERITY[RiskLevel.HIGH] > RiskLevel.SEVERITY[RiskLevel.MID]
    assert RiskLevel.SEVERITY[RiskLevel.MID] > RiskLevel.SEVERITY[RiskLevel.LOW]
    assert RiskLevel.SEVERITY[RiskLevel.LOW] > RiskLevel.SEVERITY[RiskLevel.NONE]

    # 状态机相关
    assert ReviewStatus.COMPLETED in ReviewStatus.FINAL
    assert ReviewStatus.BLOCKED not in ReviewStatus.FINAL, 'blocked 可重试，不是终态'
    assert SourceType.UPLOAD not in SourceType.APPROVE_SOURCES

    print('db.constants 自测通过 ✅')

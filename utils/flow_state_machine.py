'''utils/flow_state_machine.py'''

"""
流程状态机 —— 任务主状态机 + 回写状态机（SPEC 5.1 / 5.2 / 5.3）

为什么需要这个组件（SPEC 5.2 原文）:
    「非法转移一律拒绝并记日志；状态机由单一组件（Flow StateMachine）实现，
      **禁止散落各处直接改状态字段**。」
    所以状态迁移必须收敛到这里：各节点只声明"我发什么事件"，由本组件决定
    "能不能迁、迁到哪"，避免每个节点自己 `task.review_status = 'xxx'`。

两张状态机:

    主状态机（SPEC 5.1）
        pending --PARSE_START--> parsing --PARSE_OK--> reviewing --REVIEW_OK--> completed
        parsing --PARSE_FAIL--> blocked      reviewing --REVIEW_FAIL--> blocked
        blocked --RETRY--> pending
        CREATE: 无前置状态 → pending

    回写状态机（SPEC 5.3）
        not_written --WRITE_START--> writing --WRITE_OK--> success
        writing --WRITE_FAIL--> failed       failed --RETRY_WRITE--> writing
        注意：回写状态机与主状态机**完全独立** —— 回写失败不回退主状态（SPEC 11.1）。

设计取舍:
    为什么用纯函数式的 `transition(current, event) -> new_status` 而不是
    `apply(task, event)` 直接改 ORM 对象？
    因为要能脱离数据库做自测，也要能被 service 层、后台任务、CLI 复用。
    改状态由调用方落库（`row.review_status = FlowStateMachine.main_transition(...)`），
    状态合法性判断则只有这一个来源。

幂等性:
    若当前状态已经等于该事件的目标状态，视为重复投递，直接返回目标状态而不报错。
    这样"重跑审查""重复点击写回"这类场景不会因为状态已推进而炸掉。

异常分层:
    `FlowStateError` 属于基础设施层（同 `utils/llm_utils.LLMProviderError`），
    各 processor 捕获后按业务语义包装成自己的异常。
"""

from typing import Dict, List, Optional, Tuple
import logging

from db.constants import ReviewStatus, TaskEvent, WritebackStatus

logger = logging.getLogger(__name__)


class FlowStateError(Exception):
    """状态迁移非法（基础设施层，跨 processor 共用）"""

    def __init__(self, message: str, current_status: str = '', event: str = ''):
        self.current_status = current_status
        self.event = event
        super().__init__(message)


# ============================================================
# 转移表：事件 → (源状态, 目标状态)
# ============================================================
# 主状态机（SPEC 5.2 的转移表）
# 源状态为 None 表示"无前置条件"（仅 CREATE 这类创建事件）
MAIN_TRANSITIONS: Dict[str, Tuple[Optional[str], str]] = {
    TaskEvent.CREATE: (None, ReviewStatus.PENDING),
    TaskEvent.PARSE_START: (ReviewStatus.PENDING, ReviewStatus.PARSING),
    TaskEvent.PARSE_OK: (ReviewStatus.PARSING, ReviewStatus.REVIEWING),
    TaskEvent.PARSE_FAIL: (ReviewStatus.PARSING, ReviewStatus.BLOCKED),
    TaskEvent.REVIEW_OK: (ReviewStatus.REVIEWING, ReviewStatus.COMPLETED),
    TaskEvent.REVIEW_FAIL: (ReviewStatus.REVIEWING, ReviewStatus.BLOCKED),
    TaskEvent.RETRY: (ReviewStatus.BLOCKED, ReviewStatus.PENDING),
}

# 回写状态机（SPEC 5.3 的转移表）
WRITEBACK_TRANSITIONS: Dict[str, Tuple[Optional[str], str]] = {
    TaskEvent.WRITE_START: (WritebackStatus.NOT_WRITTEN, WritebackStatus.WRITING),
    TaskEvent.WRITE_OK: (WritebackStatus.WRITING, WritebackStatus.SUCCESS),
    TaskEvent.WRITE_FAIL: (WritebackStatus.WRITING, WritebackStatus.FAILED),
    TaskEvent.RETRY_WRITE: (WritebackStatus.FAILED, WritebackStatus.WRITING),
}

# 两张状态机各自的事件集合（用于判断一个事件该走哪张表）
_MAIN_EVENTS = frozenset(MAIN_TRANSITIONS.keys())
_WRITEBACK_EVENTS = frozenset(WRITEBACK_TRANSITIONS.keys())

# 终态（SPEC 5.1 / 5.3）—— 终态不可再迁出，便于接入层给出更准确的报错
TERMINAL_MAIN_STATES = frozenset(ReviewStatus.FINAL)
TERMINAL_WRITEBACK_STATES = frozenset(WritebackStatus.FINAL)

# 主流程的线性推进段（不含 completed / blocked 这两个"出口"）
# 用途：`resolve_main_transition` 判断当前状态是否只是"落后"于目标事件，
#      从而允许一次性补齐前置事件（见该方法的说明）。
MAIN_FLOW: Tuple[str, ...] = (
    ReviewStatus.PENDING, ReviewStatus.PARSING, ReviewStatus.REVIEWING,
)


class FlowStateMachine:
    """
    流程状态机（单一组件）

    使用示例:
        # 主状态机：解析完成 → reviewing
        task.review_status = FlowStateMachine.main_transition(
            task.review_status, TaskEvent.PARSE_OK
        )

        # 回写状态机：开始写回 → writing
        task.writeback_status = FlowStateMachine.writeback_transition(
            task.writeback_status, TaskEvent.WRITE_START
        )
    """

    # ==================== 主状态机 ====================
    @staticmethod
    def main_transition(current_status: Optional[str], event: str) -> str:
        """
        计算主状态机迁移结果（不落库）
        :param current_status: 当前 review_status（新建任务可为 None / ''）
        :param event: 事件名（db.constants.TaskEvent）
        :return: 目标状态
        :raises FlowStateError: 事件未知，或当前状态不允许该事件
        """
        return FlowStateMachine._transition(
            current_status, event, MAIN_TRANSITIONS, '主状态机'
        )

    # ==================== 回写状态机 ====================
    @staticmethod
    def writeback_transition(current_status: Optional[str], event: str) -> str:
        """
        计算回写状态机迁移结果（不落库）
        :param current_status: 当前 writeback_status
        :param event: 事件名（WRITE_START / WRITE_OK / WRITE_FAIL / RETRY_WRITE）
        :return: 目标状态
        :raises FlowStateError: 事件未知，或当前状态不允许该事件
        """
        return FlowStateMachine._transition(
            current_status, event, WRITEBACK_TRANSITIONS, '回写状态机'
        )

    # ==================== 统一迁移实现 ====================
    @staticmethod
    def _transition(current_status: Optional[str], event: str,
                    table: Dict[str, Tuple[Optional[str], str]],
                    machine_name: str) -> str:
        """
        【内部】按转移表计算目标状态
        :param current_status: 当前状态
        :param event: 事件名
        :param table: 转移表
        :param machine_name: 状态机名称（仅用于报错文案）
        :return: 目标状态
        :raises FlowStateError: 事件未知或迁移非法
        """
        if event not in table:
            raise FlowStateError(
                message=f'{machine_name}不支持事件 {event!r}，'
                        f'支持的事件：{sorted(table.keys())}',
                current_status=current_status or '',
                event=event,
            )

        expected_from, to_status = table[event]

        # 1.无前置条件的事件（CREATE）直接放行
        if expected_from is None:
            return to_status

        # 2.幂等：已经处于目标状态 → 视为重复投递，不报错
        #    （重跑审查 / 重复点击写回 都会走到这里）
        if (current_status or '') == to_status:
            return to_status

        # 3.非法迁移：拒绝并给出"当前状态允许哪些事件"，便于排查
        if (current_status or '') != expected_from:
            allowed = FlowStateMachine.allowed_events(current_status, table)
            raise FlowStateError(
                message=f'{machine_name}非法迁移：当前状态 {current_status!r} '
                        f'不允许事件 {event!r}（该事件要求源状态为 {expected_from!r}）。'
                        f'当前状态允许的事件：{allowed}',
                current_status=current_status or '',
                event=event,
            )

        return to_status

    # ==================== 查询辅助 ====================
    @staticmethod
    def resolve_main_transition(current_status: Optional[str], event: str) -> str:
        """
        宽松主状态机迁移 —— 供"节点负责把状态推进到某一步"的场景使用

        与 `main_transition` 的差别:
            严格版要求当前状态**恰好等于**该事件的源状态；
            本方法额外允许一种情况：当前状态只是**落后**于目标事件所需的源状态
            （例如接入层还没发 PARSE_START，任务仍是 pending，就直接跑完了整个解析图）。

        为什么允许"补课":
            节点是"把状态推进到某一步"的负责人，而不是"必须看到某个精确起点"。
            把 pending → parsing → reviewing 的两跳压成一次落地，
            不改变最终状态的合法性，却能让图可以被单独重跑（单独跑解析子图时，
            没人替它发 PARSE_START）。
            但**只允许向前补，不允许跨越出口**：blocked / completed / 未知状态一律拒绝，
            因为那是真正的非法迁移（blocked 必须先 RETRY，终态不能复活）。

        幂等:
            当前状态已等于目标状态时直接返回（重跑场景）。
        :param current_status: 当前 review_status
        :param event: 事件名
        :return: 目标状态
        :raises FlowStateError: 事件未知，或当前状态既不是源状态、也不能通过补课到达
        """
        if event not in MAIN_TRANSITIONS:
            raise FlowStateError(
                message=f'主状态机不支持事件 {event!r}，支持的事件：{sorted(MAIN_TRANSITIONS.keys())}',
                current_status=current_status or '',
                event=event,
            )

        expected_from, to_status = MAIN_TRANSITIONS[event]

        # 1.无前置条件（CREATE）
        if expected_from is None:
            return to_status

        current = current_status or ''

        # 2.幂等 / 精确命中源状态
        if current == to_status or current == expected_from:
            return to_status

        # 3.落后 → 允许补课（仅限主流程线性段内，且只向前）
        if (current in MAIN_FLOW and expected_from in MAIN_FLOW
                and MAIN_FLOW.index(current) < MAIN_FLOW.index(expected_from)):
            logger.warning(
                f'[状态机] 状态落后：当前 {current!r} 不在 {event!r} 所需的源状态，'
                f'按"推进到目标"处理，补课为 {to_status!r}'
            )
            return to_status

        # 4.其余（blocked / 终态 / 未知状态 / 回退）一律拒绝
        allowed = FlowStateMachine.allowed_events(current)
        raise FlowStateError(
            message=f'主状态机非法迁移：当前状态 {current!r} 无法通过事件 {event!r} 推进到 '
                    f'{to_status!r}（该事件要求源状态为 {expected_from!r}，'
                    f'或为更早的主流程状态之一 {list(MAIN_FLOW)}）。'
                    f'当前状态允许的事件：{allowed}',
            current_status=current,
            event=event,
        )

    @staticmethod
    def allowed_events(current_status: Optional[str],
                       table: Dict[str, Tuple[Optional[str], str]] = None) -> List[str]:
        """
        列出某状态下允许的事件（用于报错文案、前端按钮置灰判断）
        :param current_status: 当前状态
        :param table: 转移表，默认主状态机
        :return: 允许的事件名列表（含"无前置条件"的事件）
        """
        table = table if table is not None else MAIN_TRANSITIONS
        status = current_status or ''
        events: List[str] = []
        for event, (expected_from, to_status) in table.items():
            if expected_from is None or status == expected_from or status == to_status:
                events.append(event)
        return events

    @staticmethod
    def is_terminal(current_status: Optional[str], main: bool = True) -> bool:
        """
        判断是否处于终态
        :param current_status: 当前状态
        :param main: True 查主状态机终态，False 查回写状态机终态
        :return: 是否终态
        """
        terminal = TERMINAL_MAIN_STATES if main else TERMINAL_WRITEBACK_STATES
        return (current_status or '') in terminal

    @staticmethod
    def events_of(event: str) -> str:
        """
        判断事件属于哪张状态机（供接入层分发）
        :param event: 事件名
        :return: 'main' / 'writeback' / ''
        """
        if event in _MAIN_EVENTS:
            return 'main'
        if event in _WRITEBACK_EVENTS:
            return 'writeback'
        return ''


# ==================== 便捷包装（不落库，仅算状态）====================


def next_main_status(current_status: Optional[str], event: str) -> str:
    """主状态机迁移的简写"""
    return FlowStateMachine.main_transition(current_status, event)


def next_writeback_status(current_status: Optional[str], event: str) -> str:
    """回写状态机迁移的简写"""
    return FlowStateMachine.writeback_transition(current_status, event)


if __name__ == '__main__':
    # 轻量自测（不依赖数据库）
    passed = 0

    # ---------- 一、主状态机全路径 ----------
    status = None
    for event, expected in (
        (TaskEvent.CREATE, ReviewStatus.PENDING),
        (TaskEvent.PARSE_START, ReviewStatus.PARSING),
        (TaskEvent.PARSE_OK, ReviewStatus.REVIEWING),
        (TaskEvent.REVIEW_OK, ReviewStatus.COMPLETED),
    ):
        status = FlowStateMachine.main_transition(status, event)
        assert status == expected, f'{event} 应迁到 {expected}，实际 {status}'
    passed += 1
    print('  ✅ 主状态机正常路径 pending → parsing → reviewing → completed')

    # 阻塞分支
    assert FlowStateMachine.main_transition('parsing', TaskEvent.PARSE_FAIL) == 'blocked'
    assert FlowStateMachine.main_transition('reviewing', TaskEvent.REVIEW_FAIL) == 'blocked'
    assert FlowStateMachine.main_transition('blocked', TaskEvent.RETRY) == 'pending'
    # 重试后可重走一遍
    retried = FlowStateMachine.main_transition('blocked', TaskEvent.RETRY)
    assert FlowStateMachine.main_transition(retried, TaskEvent.PARSE_START) == 'parsing'
    passed += 1
    print('  ✅ 主状态机阻塞与重试：parsing/reviewing → blocked → pending → parsing')

    # ---------- 二、非法迁移必须被拒 ----------
    illegal_cases = [
        ('pending', TaskEvent.PARSE_OK, 'pending 不能直接到 reviewing'),
        ('pending', TaskEvent.REVIEW_OK, '未审查不能 completed'),
        ('parsing', TaskEvent.REVIEW_OK, '解析中不能直接审查完成'),
        ('completed', TaskEvent.PARSE_START, '终态不能再开工'),
        ('completed', TaskEvent.RETRY, 'completed 不是可重试状态'),
        ('reviewing', TaskEvent.PARSE_START, '不能回退到 parsing'),
        ('', TaskEvent.PARSE_OK, '状态为空不能直接 PARSE_OK'),
    ]
    for current, event, label in illegal_cases:
        try:
            FlowStateMachine.main_transition(current, event)
        except FlowStateError as e:
            assert event in str(e), f'{label}：报错应包含事件名'
            assert '允许的事件' in str(e), f'{label}：报错应给出允许的事件'
        else:
            raise AssertionError(f'{label} —— 非法迁移未被拒绝')
    passed += 1
    print('  ✅ 7 组非法迁移全部被拒绝，且报错给出"当前状态允许的事件"')

    # 未知事件
    try:
        FlowStateMachine.main_transition('pending', 'NOT_AN_EVENT')
    except FlowStateError as e:
        assert 'NOT_AN_EVENT' in str(e)
    else:
        raise AssertionError('未知事件应被拒绝')
    passed += 1
    print('  ✅ 未知事件被拒绝')

    # ---------- 三、幂等：重复投递同一事件不报错 ----------
    assert FlowStateMachine.main_transition('reviewing', TaskEvent.PARSE_OK) == 'reviewing', \
        '已在目标状态时应幂等返回'
    assert FlowStateMachine.main_transition('completed', TaskEvent.REVIEW_OK) == 'completed'
    passed += 1
    print('  ✅ 重复投递同一事件 → 幂等返回目标状态，不报错')

    # ---------- 四、回写状态机 ----------
    wb = FlowStateMachine.writeback_transition('not_written', TaskEvent.WRITE_START)
    assert wb == 'writing', f'WRITE_START 应到 writing，实际 {wb}'
    assert FlowStateMachine.writeback_transition(wb, TaskEvent.WRITE_OK) == 'success'
    assert FlowStateMachine.writeback_transition(wb, TaskEvent.WRITE_FAIL) == 'failed'
    assert FlowStateMachine.writeback_transition('failed', TaskEvent.RETRY_WRITE) == 'writing'
    passed += 1
    print('  ✅ 回写状态机 not_written → writing → success / failed → writing')

    # 回写状态机的非法迁移
    for current, event in (('not_written', TaskEvent.WRITE_OK),
                           ('success', TaskEvent.WRITE_START),
                           ('success', TaskEvent.RETRY_WRITE)):
        try:
            FlowStateMachine.writeback_transition(current, event)
        except FlowStateError as e:
            assert '回写状态机' in str(e), '报错应标明是回写状态机'
        else:
            raise AssertionError(f'回写状态机 {current} -{event}-> 应被拒绝')
    # writing 状态下再次 RETRY_WRITE：目标状态就是 writing，按幂等处理（不报错）
    # —— 管理员在回写进行中重复点"重试"时，静默忽略比报错更合理。
    assert FlowStateMachine.writeback_transition('writing', TaskEvent.RETRY_WRITE) == 'writing'
    passed += 1
    print('  ✅ 回写状态机非法迁移（含 success 终态）全部被拒绝；writing 重复重试按幂等处理')

    # ---------- 五、两张状态机不串味 ----------
    # 回写事件不能走主状态机，反之亦然
    for event in (TaskEvent.WRITE_START, TaskEvent.WRITE_OK, TaskEvent.WRITE_FAIL,
                  TaskEvent.RETRY_WRITE):
        try:
            FlowStateMachine.main_transition('completed', event)
        except FlowStateError:
            pass
        else:
            raise AssertionError(f'回写事件 {event} 不应被主状态机接受')
    try:
        FlowStateMachine.writeback_transition('not_written', TaskEvent.REVIEW_OK)
    except FlowStateError:
        pass
    else:
        raise AssertionError('主状态机事件不应被回写状态机接受')
    passed += 1
    print('  ✅ 两张状态机事件互不串用')

    # ---------- 五点五、宽松迁移 resolve_main_transition ----------
    print('  --- 宽松迁移（节点负责"推进到某一步"）---')
    # 补课：接入层没发 PARSE_START，任务还是 pending，解析图直接跑完
    assert FlowStateMachine.resolve_main_transition('pending', TaskEvent.PARSE_OK) == 'reviewing'
    # 精确命中源状态
    assert FlowStateMachine.resolve_main_transition('parsing', TaskEvent.PARSE_OK) == 'reviewing'
    # 幂等
    assert FlowStateMachine.resolve_main_transition('reviewing', TaskEvent.PARSE_OK) == 'reviewing'
    assert FlowStateMachine.resolve_main_transition(None, TaskEvent.CREATE) == 'pending'
    # 只允许向"前"补课，禁止跨越出口
    for current, event, label in (
        ('blocked', TaskEvent.PARSE_OK, 'blocked 必须先 RETRY'),
        ('completed', TaskEvent.PARSE_OK, '终态不能复活'),
        ('reviewing', TaskEvent.PARSE_START, '不允许回退到 parsing'),
    ):
        try:
            FlowStateMachine.resolve_main_transition(current, event)
        except FlowStateError:
            pass
        else:
            raise AssertionError(f'{label} —— 应被拒绝')
    passed += 1
    print('  ✅ 宽松迁移：允许向前补课，blocked/终态/回退严格拒绝')

    # ---------- 六、终态与辅助查询 ----------    assert FlowStateMachine.is_terminal('completed') is True
    assert FlowStateMachine.is_terminal('blocked') is False, 'blocked 可重试，不是终态'
    assert FlowStateMachine.is_terminal('success', main=False) is True
    assert FlowStateMachine.is_terminal('failed', main=False) is False

    assert 'PARSE_START' in FlowStateMachine.allowed_events('pending')
    assert 'REVIEW_OK' not in FlowStateMachine.allowed_events('pending')
    assert FlowStateMachine.events_of(TaskEvent.PARSE_OK) == 'main'
    assert FlowStateMachine.events_of(TaskEvent.WRITE_OK) == 'writeback'
    assert FlowStateMachine.events_of('NOPE') == ''
    passed += 1
    print('  ✅ 终态判断 / allowed_events / events_of 均正确')

    # ---------- 七、转移表与 SPEC 常量对齐 ----------
    assert set(MAIN_TRANSITIONS.keys()) <= set(TaskEvent.ALL), \
        '主状态机事件必须都在 TaskEvent 常量里'
    assert set(WRITEBACK_TRANSITIONS.keys()) <= set(TaskEvent.ALL), \
        '回写状态机事件必须都在 TaskEvent 常量里'
    for event, (expected_from, to_status) in MAIN_TRANSITIONS.items():
        assert to_status in ReviewStatus.ALL, f'{event} 的目标状态 {to_status} 非法'
        assert expected_from is None or expected_from in ReviewStatus.ALL, \
            f'{event} 的源状态 {expected_from} 非法'
    for event, (expected_from, to_status) in WRITEBACK_TRANSITIONS.items():
        assert to_status in WritebackStatus.ALL, f'{event} 的目标状态 {to_status} 非法'
    passed += 1
    print('  ✅ 转移表取值与 db.constants 完全对齐')

    print(f'flow_state_machine 自测全部通过 ✅ 共 {passed} 个用例')

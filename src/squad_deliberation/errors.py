"""议事库领域错误。"""

from __future__ import annotations


class DomainError(Exception):
    """所有议事规则违反的基类。"""


class AuthorizationError(DomainError):
    """当前角色无权执行该操作（如教练解除医疗限制）。"""


class ContractViolation(DomainError):
    """事件不满足领域契约。"""

    def __init__(self, issues: list[str]):
        super().__init__("；".join(issues))
        self.issues = issues


class ConcurrentModification(DomainError):
    """聚合版本与提交方预期不一致（并行修改）。"""


class DuplicateConflict(DomainError):
    """相同事件标识携带了不同内容。"""


class DeliberationBlocked(DomainError):
    """终审未通过，携带全部阻断原因（而非只报第一个）。"""

    def __init__(self, reasons: list[str]):
        super().__init__("终审被阻断：" + "；".join(reasons))
        self.reasons = reasons


class AlreadyLocked(DomainError):
    """该比赛已有锁定阵容；重复确认应沿用原回执。"""

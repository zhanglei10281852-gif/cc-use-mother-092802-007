"""领域错误。"""


class ResilienceError(Exception):
    """所有规划领域错误的基类。"""


class InvalidPlanningData(ResilienceError):
    """输入数据未通过完整性校验。"""


class UnknownReference(InvalidPlanningData):
    """引用了不存在的资产、服务点、情景或项目。"""


class CyclicDependency(InvalidPlanningData):
    """资产供电网或工程前置关系中出现了环。"""


class InvalidTransition(ResilienceError):
    """计划状态流转非法（如撤回非当前已审批版本）。"""


class NotFoundError(ResilienceError):
    """计划或版本不存在（HTTP 404）。"""

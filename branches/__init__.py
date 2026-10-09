# ============================================================
# BoTon v4.0 — реестр филиалов
# ============================================================

from .base import Branch
from .branch_107_f2 import BRANCH_107_F2


REGISTRY = {
    BRANCH_107_F2.code: BRANCH_107_F2,
    # При добавлении филиала — раскомментируйте:
    # from .branch_48 import BRANCH_48
    # BRANCH_48.code: BRANCH_48,
}


def get_branch(code: str) -> Branch:
    if code not in REGISTRY:
        raise ValueError(
            f"Филиал '{code}' не найден. Доступные: {list(REGISTRY)}"
        )
    return REGISTRY[code]


def all_branches():
    return list(REGISTRY.values())

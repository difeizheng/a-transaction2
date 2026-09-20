"""数据新鲜度徽标：统一「数据截至 + 新旧程度」展示。

约定（与 data_mgmt 页口径一致）：
- 🟢 ≤1 个自然日；🟡 ≤5 日；🔴 >5 日（日频数据）
- 财报等季频数据不走本组件（季报口径见 data_mgmt）
"""
from datetime import date, datetime


def freshness_badge(dt_str: str, label: str = "数据截至") -> str:
    """返回 markdown 徽标文本，如 `🟡 数据截至 2026-09-17（3 天前）`。"""
    if not dt_str:
        return f"⚪ {label}未知"
    try:
        d = datetime.fromisoformat(str(dt_str)[:10]).date()
    except ValueError:
        return f"⚪ {label} {dt_str}"
    age = (date.today() - d).days
    if age <= 1:
        dot = "🟢"
    elif age <= 5:
        dot = "🟡"
    else:
        dot = "🔴"
    ago = "今天" if age == 0 else ("昨天" if age == 1 else f"{age} 天前")
    return f"{dot} {label} {d.isoformat()}（{ago}）"

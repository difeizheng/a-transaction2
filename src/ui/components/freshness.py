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


def bars_freshness_badge(storage, label: str = "K线数据截至") -> str:
    """覆盖率口径的 K 线新鲜度徽标：`🟡 K线数据截至 2026-09-17（4 天前）· 覆盖 88.4%`。

    为什么不用 MAX(trade_date)：单只股票被深度分析等路径顺带补拉后，
    MAX 口径会把全局「最新日期」带偏一天（第四轮巡检实证：5868 只中仅 1 只
    有 09-18 数据，徽标却显示「截至 09-18」）。覆盖率口径取「≥80% 股票已更新
    到的最新日期」，无达标时退化为覆盖率最高日期。"""
    try:
        res = storage.get_bars_coverage_date(min_coverage=0.8)
    except Exception:
        res = None
    if not res:
        return freshness_badge(None, label)
    d, cov = res
    return f"{freshness_badge(d, label)} · 覆盖 {cov * 100:.1f}%"

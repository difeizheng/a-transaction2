"""财务数据 point-in-time（PIT）过滤（纯函数）。

**这是回测「用未来信息决策」的头号来源**：财务报告的「报告期」(report_date，如季末)
与「披露日」(ann_date，实际公告日) 之间有 1~4 个月窗口。在 report_date 当天就用了
这份季报的 PE/ROE/增速去打分，等于偷看了 2 个月后才公告的数据 —— 价值/成长策略收益
系统性高估（见 docs/投资系统审计报告.md P1-A）。

PIT 铁律：在决策日 t，只能用 ``ann_date <= t``（已公告）的财务数据。

本模块提供纯函数过滤，供回测/选股在任一决策日取「当时已披露的最新财报」。当
``ann_date`` 缺失（akshare 无该字段）时，按报告期 + 保守披露延迟兜底，仍施加 PIT 纪律
（不会退化成「报告期当天即可用」）。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

# 季报披露的保守延迟（天）：A股一季报/年报 4 月底前、半年报 8 月底、三季报 10 月底。
# 取 90 天作为「无 ann_date 时的披露窗口兜底」——略保守但不丢大部分数据。
DEFAULT_DISCLOSURE_LAG_DAYS = 90


def _to_date(value) -> Optional[pd.Timestamp]:
    """容错转日期：字符串/Timestamp/None → Timestamp 或 None。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
        return ts if pd.notna(ts) else None
    except Exception:
        return None


def filter_point_in_time(
    fin_df: pd.DataFrame,
    as_of_date,
    ann_col: str = "ann_date",
    report_col: str = "report_date",
    default_lag_days: int = DEFAULT_DISCLOSURE_LAG_DAYS,
) -> pd.DataFrame:
    """返回在 ``as_of_date`` 当天**已披露**的财务记录（按 report_date 降序）。

    判定规则（逐行）：
    - ``ann_date`` 存在且 <= as_of：保留；
    - ``ann_date`` 存在且 >  as_of：丢弃（尚未公告，用了即前视）；
    - ``ann_date`` 缺失：按 ``report_date + default_lag_days`` 估计披露日，
      估计披露日 <= as_of 才保留（保守延迟兜底，仍施加 PIT 纪律）。

    返回的 DataFrame 按 ``report_date`` 降序——第一行即「决策日当时最新的已披露财报」。
    空输入返回空 DataFrame。
    """
    if fin_df is None or fin_df.empty:
        return pd.DataFrame()

    df = fin_df.copy()
    as_of = _to_date(as_of_date)
    if as_of is None:
        # 决策日本身无效：保守返回空（不能做 PIT 判定就不给数据）
        return pd.DataFrame()

    has_ann = ann_col in df.columns
    has_report = report_col in df.columns
    if not has_report:
        # 无报告期列无法施加 PIT 兜底 —— 保守返回空
        return pd.DataFrame()

    keep_mask = []
    for _, row in df.iterrows():
        ann = _to_date(row[ann_col]) if has_ann else None
        report = _to_date(row[report_col])
        if ann is not None:
            disclosed = ann
        elif report is not None:
            disclosed = report + pd.Timedelta(days=default_lag_days)
        else:
            keep_mask.append(False)
            continue
        keep_mask.append(disclosed <= as_of)

    filtered = df[pd.Series(keep_mask, index=df.index)]
    return filtered.sort_values(report_col, ascending=False).reset_index(drop=True)


def latest_as_of(
    fin_df: pd.DataFrame,
    as_of_date,
    ann_col: str = "ann_date",
    report_col: str = "report_date",
    default_lag_days: int = DEFAULT_DISCLOSURE_LAG_DAYS,
) -> Optional[pd.Series]:
    """决策日当时最新的**已披露**财报行；无可用则 None。"""
    filtered = filter_point_in_time(
        fin_df, as_of_date, ann_col=ann_col, report_col=report_col,
        default_lag_days=default_lag_days,
    )
    if filtered.empty:
        return None
    return filtered.iloc[0]


def latest_as_of_batch(
    fin_by_code: dict,
    as_of_date,
    **kwargs,
) -> dict:
    """批量：{code: fin_df} → {code: 最新已披露财报行}（无则该 code 不在结果中）。

    供回测/批量选股在某一决策日对全股票池取 PIT 财报，替代旧的「取最新 report_date」
    （后者无视披露日，是前视偏差）。
    """
    out = {}
    for code, df in fin_by_code.items():
        row = latest_as_of(df, as_of_date, **kwargs)
        if row is not None:
            out[code] = row
    return out

"""市场指数代码与腾讯备源 symbol 映射（数据获取层配置）。

这些是「拉哪些指数」的**数据获取**配置，逻辑上属 data 层。``DataManager.get_market_snapshot``
（data 层）与 ``src.analysis.sentiment``（analysis 层）都依赖它——放在 data 层使依赖
方向正确：analysis → data（正向），而非 data → analysis（反向依赖，旧实现的问题）。

注意：``INDEX_WEIGHTS`` 等情绪**算法**权重不在此处——它们是 sentiment 计算逻辑的一部分，
留在 ``src/analysis/sentiment.py``。
"""

# 跟踪的指数代码与中文名（DataManager.get_market_snapshot 与 sentiment 共用）
INDEX_CODES = {
    "000300": "沪深300",
    "000001": "上证指数",
    "399006": "创业板指",
    "000905": "中证500",
}

# 腾讯实时行情指数 symbol 映射（东财日K push2 被限流时的备源）。
# 不能套用个股规则（6→sh/其他→sz）——000001 在指数里是上证指数(sh)而非平安银行(sz)，
# 必须显式映射交易所前缀。
INDEX_TENCENT_SYMBOLS = {
    "000300": "sh000300",  # 沪深300（上交所）
    "000001": "sh000001",  # 上证指数（上交所）
    "399006": "sz399006",  # 创业板指（深交所）
    "000905": "sh000905",  # 中证500（上交所）
}

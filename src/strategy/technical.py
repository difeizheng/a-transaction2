"""技术分析选股策略"""
import logging
from typing import List
import pandas as pd
import pandas_ta as ta

from src.strategy.base import BaseStrategy, ScreenResult, StockEvaluation, ConditionCheck, ExitSignal

logger = logging.getLogger(__name__)


def _calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """在日K线DataFrame上计算常用技术指标"""
    df = df.copy().sort_values("trade_date").reset_index(drop=True)
    df["ma5"] = ta.sma(df["close"], length=5)
    df["ma10"] = ta.sma(df["close"], length=10)
    df["ma20"] = ta.sma(df["close"], length=20)
    df["ma60"] = ta.sma(df["close"], length=60)
    macd = ta.macd(df["close"])
    if macd is not None:
        df = pd.concat([df, macd], axis=1)
    kdj = ta.stoch(df["high"], df["low"], df["close"])
    if kdj is not None:
        df = pd.concat([df, kdj], axis=1)
    bbands = ta.bbands(df["close"], length=20)
    if bbands is not None:
        df = pd.concat([df, bbands], axis=1)
    return df


class MACrossStrategy(BaseStrategy):
    """均线多头排列策略：MA5>MA10>MA20>MA60，且价格站上MA20"""

    name = "ma_cross"
    description = "均线多头排列（MA5>MA10>MA20>MA60）"

    def __init__(self, min_bars: int = 60):
        self.min_bars = min_bars

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        results = []
        total = len(stock_pool)
        for i, (_, row) in enumerate(stock_pool.iterrows(), 1):
            code, name = row["code"], row.get("name", "")
            if progress_callback:
                progress_callback(i, total, code, name)
            try:
                df = data_manager.get_daily_bars(code)
                if len(df) < self.min_bars:
                    continue
                df = _calc_indicators(df)
                last = df.iloc[-1]
                if pd.isna(last["ma60"]):
                    continue
                # 多头排列条件
                bull = (last["ma5"] > last["ma10"] > last["ma20"] > last["ma60"]
                        and last["close"] > last["ma20"])
                if not bull:
                    continue
                # 评分：MA5相对MA60的距离越小越安全，但趋势越强越好
                score = (last["ma5"] / last["ma60"] - 1) * 100
                results.append(ScreenResult(
                    code=code, name=name, score=round(score, 2),
                    signals={"ma5": last["ma5"], "ma20": last["ma20"], "ma60": last["ma60"]},
                    reason="均线多头排列，价格站上MA20"
                ))
            except Exception as e:
                logger.debug(f"{code} MA策略计算失败: {e}")
        return sorted(results, key=lambda x: x.score, reverse=True)

    def get_params(self) -> dict:
        return {"min_bars": self.min_bars}

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        trace = []
        try:
            df = data_manager.get_daily_bars(code)
            trace.append(f"K线数据: {len(df)} 条")
            if len(df) < self.min_bars:
                return StockEvaluation(
                    code=code, name=name, strategy_key="ma_cross",
                    selected=False, score=0, reason=f"K线不足{self.min_bars}条",
                    trace_log="\n".join(trace)
                )
            df = _calc_indicators(df)
            last = df.iloc[-1]
            if pd.isna(last["ma60"]):
                return StockEvaluation(
                    code=code, name=name, strategy_key="ma_cross",
                    selected=False, score=0, reason="MA60数据不足",
                    trace_log="\n".join(trace)
                )
            ma5, ma10, ma20, ma60 = float(last["ma5"]), float(last["ma10"]), float(last["ma20"]), float(last["ma60"])
            close = float(last["close"])
            trace.append(f"MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f} MA60={ma60:.2f} close={close:.2f}")
            cond1 = ma5 > ma10 > ma20 > ma60
            cond2 = close > ma20
            conditions = [
                ConditionCheck("MA5>MA10>MA20>MA60", cond1,
                               f"{ma5:.2f}>{ma10:.2f}>{ma20:.2f}>{ma60:.2f}"),
                ConditionCheck("close>MA20", cond2, f"{close:.2f}>{ma20:.2f}"),
            ]
            selected = cond1 and cond2
            score = round((ma5 / ma60 - 1) * 100, 4) if selected else 0
            return StockEvaluation(
                code=code, name=name, strategy_key="ma_cross",
                selected=selected, score=score,
                indicators={"MA5": round(ma5, 2), "MA10": round(ma10, 2),
                            "MA20": round(ma20, 2), "MA60": round(ma60, 2), "close": round(close, 2)},
                conditions=conditions,
                reason="均线多头排列，价格站上MA20" if selected else "不满足均线多头排列条件",
                trace_log="\n".join(trace)
            )
        except Exception as e:
            return StockEvaluation(
                code=code, name=name, strategy_key="ma_cross",
                selected=False, score=0, reason=f"计算异常: {e}",
                trace_log="\n".join(trace)
            )

    def evaluate_exit(self, code: str, name: str, data_manager) -> ExitSignal:
        """对称出场：入场要求多头排列（MA5>MA10>MA20>MA60 且 close>MA20）；
        出场即趋势破位——close 跌破 MA20，或 MA5 下穿 MA10（短期转弱）。"""
        try:
            df = data_manager.get_daily_bars(code)
            if len(df) < self.min_bars:
                return ExitSignal(code=code, name=name, strategy_key="ma_cross",
                                  should_exit=False, reason="K线不足")
            df = _calc_indicators(df)
            last = df.iloc[-1]
            if pd.isna(last["ma60"]) or pd.isna(last["ma20"]) or pd.isna(last["ma10"]) or pd.isna(last["ma5"]):
                return ExitSignal(code=code, name=name, strategy_key="ma_cross",
                                  should_exit=False, reason="均线数据不足")
            ma5, ma10, ma20 = float(last["ma5"]), float(last["ma10"]), float(last["ma20"])
            close = float(last["close"])
            break_support = close < ma20        # 跌破 MA20 支撑
            short_reversal = ma5 <= ma10        # 短期均线反向
            should_exit = break_support or short_reversal
            reason = []
            if break_support:
                reason.append(f"close {close:.2f} 跌破 MA20 {ma20:.2f}")
            if short_reversal:
                reason.append(f"MA5 {ma5:.2f} ≤ MA10 {ma10:.2f}")
            return ExitSignal(
                code=code, name=name, strategy_key="ma_cross",
                should_exit=should_exit,
                reason=("趋势破位：" + "；".join(reason)) if should_exit else "多头排列未破",
                indicators={"MA5": round(ma5, 2), "MA10": round(ma10, 2),
                            "MA20": round(ma20, 2), "close": round(close, 2)},
            )
        except Exception as e:
            return ExitSignal(code=code, name=name, strategy_key="ma_cross",
                              should_exit=False, reason=f"计算异常: {e}")

class MACDGoldenCrossStrategy(BaseStrategy):
    """MACD金叉策略：DIF上穿DEA，且MACD柱由负转正"""

    name = "macd_golden"
    description = "MACD金叉（DIF上穿DEA）"

    def __init__(self, lookback: int = 3):
        self.lookback = lookback  # 最近N天内出现金叉

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        results = []
        total = len(stock_pool)
        for i, (_, row) in enumerate(stock_pool.iterrows(), 1):
            code, name = row["code"], row.get("name", "")
            if progress_callback:
                progress_callback(i, total, code, name)
            try:
                df = data_manager.get_daily_bars(code)
                if len(df) < 60:
                    continue
                df = _calc_indicators(df)
                # pandas_ta MACD列名: MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
                dif_col = [c for c in df.columns if c.startswith("MACD_") and "h" not in c and "s" not in c]
                dea_col = [c for c in df.columns if c.startswith("MACDs_")]
                if not dif_col or not dea_col:
                    continue
                dif, dea = df[dif_col[0]], df[dea_col[0]]
                # 检查最近N天内是否有金叉
                golden = False
                for j in range(-self.lookback, 0):
                    if (dif.iloc[j] > dea.iloc[j]) and (dif.iloc[j-1] <= dea.iloc[j-1]):
                        golden = True
                        break
                if not golden:
                    continue
                # 信号有效性：当前DIF仍须在DEA上方（金叉后未死叉回落才有效）
                if not (dif.iloc[-1] > dea.iloc[-1]):
                    continue
                last_dif = dif.iloc[-1]
                score = float(last_dif) if not pd.isna(last_dif) else 0
                results.append(ScreenResult(
                    code=code, name=name, score=round(score, 4),
                    signals={"dif": round(float(dif.iloc[-1]), 4), "dea": round(float(dea.iloc[-1]), 4)},
                    reason=f"近{self.lookback}日内MACD金叉"
                ))
            except Exception as e:
                logger.debug(f"{code} MACD策略计算失败: {e}")
        return sorted(results, key=lambda x: x.score, reverse=True)

    def get_params(self) -> dict:
        return {"lookback": self.lookback}

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        trace = []
        try:
            df = data_manager.get_daily_bars(code)
            trace.append(f"K线数据: {len(df)} 条")
            if len(df) < 60:
                return StockEvaluation(code=code, name=name, strategy_key="macd_golden",
                                       selected=False, score=0, reason="K线不足60条",
                                       trace_log="\n".join(trace))
            df = _calc_indicators(df)
            dif_col = [c for c in df.columns if c.startswith("MACD_") and "h" not in c and "s" not in c]
            dea_col = [c for c in df.columns if c.startswith("MACDs_")]
            if not dif_col or not dea_col:
                return StockEvaluation(code=code, name=name, strategy_key="macd_golden",
                                       selected=False, score=0, reason="MACD指标计算失败",
                                       trace_log="\n".join(trace))
            dif, dea = df[dif_col[0]], df[dea_col[0]]
            dif_val, dea_val = round(float(dif.iloc[-1]), 4), round(float(dea.iloc[-1]), 4)
            trace.append(f"DIF={dif_val} DEA={dea_val}")
            golden = False
            cross_day = None
            for j in range(-self.lookback, 0):
                if (dif.iloc[j] > dea.iloc[j]) and (dif.iloc[j-1] <= dea.iloc[j-1]):
                    golden = True
                    cross_day = j
                    break
            cond1 = golden
            cond2 = dif_val > 0
            cond3 = dif_val > dea_val  # 信号未失效：当前仍处金叉状态
            conditions = [
                ConditionCheck(f"近{self.lookback}日内DIF上穿DEA", cond1,
                               f"第{cross_day}日金叉" if cross_day else "无金叉"),
                ConditionCheck("DIF>0（零轴上方）", cond2, f"DIF={dif_val}"),
                ConditionCheck("当前DIF>DEA（信号未失效）", cond3, f"DIF={dif_val}>DEA={dea_val}"),
            ]
            selected = cond1 and cond3
            score = round(dif_val, 4) if selected else 0
            return StockEvaluation(
                code=code, name=name, strategy_key="macd_golden",
                selected=selected, score=score,
                indicators={"DIF": dif_val, "DEA": dea_val},
                conditions=conditions,
                reason=f"近{self.lookback}日内MACD金叉" if selected else "未出现MACD金叉",
                trace_log="\n".join(trace)
            )
        except Exception as e:
            return StockEvaluation(code=code, name=name, strategy_key="macd_golden",
                                   selected=False, score=0, reason=f"计算异常: {e}",
                                   trace_log="\n".join(trace))


    def evaluate_exit(self, code: str, name: str, data_manager) -> ExitSignal:
        """对称出场：入场要求 DIF>DEA（金叉后信号有效）；出场即 DIF 下穿 DEA（死叉）——
        与入场「当前 DIF>DEA」严格对称反向。"""
        try:
            df = data_manager.get_daily_bars(code)
            if len(df) < 60:
                return ExitSignal(code=code, name=name, strategy_key="macd_golden",
                                  should_exit=False, reason="K线不足60条")
            df = _calc_indicators(df)
            dif_col = [c for c in df.columns if c.startswith("MACD_") and "h" not in c and "s" not in c]
            dea_col = [c for c in df.columns if c.startswith("MACDs_")]
            if not dif_col or not dea_col:
                return ExitSignal(code=code, name=name, strategy_key="macd_golden",
                                  should_exit=False, reason="MACD指标计算失败")
            dif, dea = df[dif_col[0]], df[dea_col[0]]
            dif_val, dea_val = float(dif.iloc[-1]), float(dea.iloc[-1])
            # 死叉：当前 DIF<=DEA（入场要求 DIF>DEA，对称反向）
            should_exit = dif_val <= dea_val
            reason = (f"DIF {dif_val:.4f} ≤ DEA {dea_val:.4f}（死叉，信号失效）"
                      if should_exit else f"DIF {dif_val:.4f} > DEA {dea_val:.4f}（信号仍在）")
            return ExitSignal(
                code=code, name=name, strategy_key="macd_golden",
                should_exit=should_exit, reason=reason,
                indicators={"DIF": round(dif_val, 4), "DEA": round(dea_val, 4)},
            )
        except Exception as e:
            return ExitSignal(code=code, name=name, strategy_key="macd_golden",
                              should_exit=False, reason=f"计算异常: {e}")


class KDJOversoldStrategy(BaseStrategy):
    """KDJ超卖反弹策略：K<30且D<30，且K上穿D

    ⚠️ **已标记为失效（is_deprecated=True）**：KDJ 是 90 年代美股指标，A 股 2017 机构化后
    大面积失效；超卖反弹在 A 股易踩「价值陷阱」（低估值继续杀估值）。保留代码供历史复现，
    但从默认策略集/UI 下拉中排除（见 screener.list_strategies active_only）。详见审计报告 P1-D。
    """

    name = "kdj_oversold"
    description = "KDJ超卖反弹（K<30且K上穿D）【已失效，默认排除】"
    is_deprecated = True
    deprecation_reason = "90年代美股指标，A股机构化后大面积失效，超卖反弹易踩价值陷阱"

    def __init__(self, oversold_threshold: int = 30, lookback: int = 3):
        self.oversold_threshold = oversold_threshold
        self.lookback = lookback

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        results = []
        total = len(stock_pool)
        for i, (_, row) in enumerate(stock_pool.iterrows(), 1):
            code, name = row["code"], row.get("name", "")
            if progress_callback:
                progress_callback(i, total, code, name)
            try:
                df = data_manager.get_daily_bars(code)
                if len(df) < 30:
                    continue
                df = _calc_indicators(df)
                k_col = [c for c in df.columns if c.startswith("STOCHk_")]
                d_col = [c for c in df.columns if c.startswith("STOCHd_")]
                if not k_col or not d_col:
                    continue
                k, d = df[k_col[0]], df[d_col[0]]
                # 检查近N天内K上穿D且处于超卖区
                cross = False
                for i in range(-self.lookback, 0):
                    if (k.iloc[i] > d.iloc[i] and k.iloc[i-1] <= d.iloc[i-1]
                            and k.iloc[i] < self.oversold_threshold):
                        cross = True
                        break
                if not cross:
                    continue
                score = self.oversold_threshold - float(k.iloc[-1])  # K越低分越高
                results.append(ScreenResult(
                    code=code, name=name, score=round(score, 2),
                    signals={"k": round(float(k.iloc[-1]), 2), "d": round(float(d.iloc[-1]), 2)},
                    reason=f"KDJ超卖区金叉（K={k.iloc[-1]:.1f}）"
                ))
            except Exception as e:
                logger.debug(f"{code} KDJ策略计算失败: {e}")
        return sorted(results, key=lambda x: x.score, reverse=True)

    def get_params(self) -> dict:
        return {"oversold_threshold": self.oversold_threshold, "lookback": self.lookback}

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        trace = []
        try:
            df = data_manager.get_daily_bars(code)
            trace.append(f"K线数据: {len(df)} 条")
            if len(df) < 30:
                return StockEvaluation(code=code, name=name, strategy_key="kdj_oversold",
                                       selected=False, score=0, reason="K线不足30条",
                                       trace_log="\n".join(trace))
            df = _calc_indicators(df)
            k_col = [c for c in df.columns if c.startswith("STOCHk_")]
            d_col = [c for c in df.columns if c.startswith("STOCHd_")]
            if not k_col or not d_col:
                return StockEvaluation(code=code, name=name, strategy_key="kdj_oversold",
                                       selected=False, score=0, reason="KDJ指标计算失败",
                                       trace_log="\n".join(trace))
            k, d = df[k_col[0]], df[d_col[0]]
            k_val, d_val = round(float(k.iloc[-1]), 2), round(float(d.iloc[-1]), 2)
            trace.append(f"K={k_val} D={d_val} 超卖阈值={self.oversold_threshold}")
            cross = False
            cross_k = None
            for j in range(-self.lookback, 0):
                if (k.iloc[j] > d.iloc[j] and k.iloc[j-1] <= d.iloc[j-1]
                        and k.iloc[j] < self.oversold_threshold):
                    cross = True
                    cross_k = round(float(k.iloc[j]), 2)
                    break
            cond1 = cross
            cond2 = k_val < self.oversold_threshold
            conditions = [
                ConditionCheck(f"近{self.lookback}日K上穿D且K<{self.oversold_threshold}", cond1,
                               f"金叉时K={cross_k}" if cross_k else "无超卖金叉"),
                ConditionCheck(f"当前K<{self.oversold_threshold}（超卖区）", cond2, f"K={k_val}"),
            ]
            selected = cond1
            score = round(self.oversold_threshold - k_val, 2) if selected else 0
            return StockEvaluation(
                code=code, name=name, strategy_key="kdj_oversold",
                selected=selected, score=score,
                indicators={"K": k_val, "D": d_val, "超卖阈值": self.oversold_threshold},
                conditions=conditions,
                reason=f"KDJ超卖区金叉（K={k_val}）" if selected else "未满足KDJ超卖金叉条件",
                trace_log="\n".join(trace)
            )
        except Exception as e:
            return StockEvaluation(code=code, name=name, strategy_key="kdj_oversold",
                                   selected=False, score=0, reason=f"计算异常: {e}",
                                   trace_log="\n".join(trace))


class BollingerBreakoutStrategy(BaseStrategy):
    """布林带突破策略：价格从下轨反弹，突破中轨

    ⚠️ **已标记为失效（is_deprecated=True）**：布林带突破在 A 股震荡市频繁假突破、趋势市
    踏空，未配合成交量/趋势过滤时胜率偏低。保留代码供历史复现，从默认策略集/UI 下拉中
    排除。详见审计报告 P1-D。
    """

    name = "boll_breakout"
    description = "布林带下轨反弹突破中轨【已失效，默认排除】"
    is_deprecated = True
    deprecation_reason = "A股震荡市频繁假突破，未配合量能/趋势过滤时胜率低"

    def __init__(self, lookback: int = 5):
        self.lookback = lookback

    def screen(self, stock_pool: pd.DataFrame, data_manager, progress_callback=None) -> List[ScreenResult]:
        results = []
        total = len(stock_pool)
        for i, (_, row) in enumerate(stock_pool.iterrows(), 1):
            code, name = row["code"], row.get("name", "")
            if progress_callback:
                progress_callback(i, total, code, name)
            try:
                df = data_manager.get_daily_bars(code)
                if len(df) < 30:
                    continue
                df = _calc_indicators(df)
                lower_col = [c for c in df.columns if c.startswith("BBL_")]
                mid_col = [c for c in df.columns if c.startswith("BBM_")]
                if not lower_col or not mid_col:
                    continue
                lower, mid = df[lower_col[0]], df[mid_col[0]]
                close = df["close"]
                touched_lower = (close.iloc[-self.lookback:] <= lower.iloc[-self.lookback:]).any()
                above_mid = close.iloc[-1] > mid.iloc[-1]
                if not (touched_lower and above_mid):
                    continue
                score = (close.iloc[-1] / mid.iloc[-1] - 1) * 100
                results.append(ScreenResult(
                    code=code, name=name, score=round(score, 2),
                    signals={"close": close.iloc[-1], "mid": round(float(mid.iloc[-1]), 2),
                             "lower": round(float(lower.iloc[-1]), 2)},
                    reason="布林带下轨反弹，突破中轨"
                ))
            except Exception as e:
                logger.debug(f"{code} 布林带策略计算失败: {e}")
        return sorted(results, key=lambda x: x.score, reverse=True)

    def get_params(self) -> dict:
        return {"lookback": self.lookback}

    def evaluate_stock(self, code: str, name: str, data_manager) -> StockEvaluation:
        trace = []
        try:
            df = data_manager.get_daily_bars(code)
            trace.append(f"K线数据: {len(df)} 条")
            if len(df) < 30:
                return StockEvaluation(code=code, name=name, strategy_key="boll_breakout",
                                       selected=False, score=0, reason="K线不足30条",
                                       trace_log="\n".join(trace))
            df = _calc_indicators(df)
            lower_col = [c for c in df.columns if c.startswith("BBL_")]
            mid_col = [c for c in df.columns if c.startswith("BBM_")]
            upper_col = [c for c in df.columns if c.startswith("BBU_")]
            if not lower_col or not mid_col:
                return StockEvaluation(code=code, name=name, strategy_key="boll_breakout",
                                       selected=False, score=0, reason="布林带指标计算失败",
                                       trace_log="\n".join(trace))
            lower, mid = df[lower_col[0]], df[mid_col[0]]
            upper = df[upper_col[0]] if upper_col else None
            close = df["close"]
            close_val = round(float(close.iloc[-1]), 2)
            mid_val = round(float(mid.iloc[-1]), 2)
            lower_val = round(float(lower.iloc[-1]), 2)
            upper_val = round(float(upper.iloc[-1]), 2) if upper is not None else None
            trace.append(f"close={close_val} 上轨={upper_val} 中轨={mid_val} 下轨={lower_val}")
            touched_lower = bool((close.iloc[-self.lookback:] <= lower.iloc[-self.lookback:]).any())
            above_mid = close_val > mid_val
            conditions = [
                ConditionCheck(f"近{self.lookback}日触及下轨", touched_lower, f"下轨={lower_val}"),
                ConditionCheck("当前价格站上中轨", above_mid, f"close={close_val} > 中轨={mid_val}"),
            ]
            selected = touched_lower and above_mid
            score = round((close_val / mid_val - 1) * 100, 4) if selected else 0
            indic = {"close": close_val, "中轨": mid_val, "下轨": lower_val}
            if upper_val:
                indic["上轨"] = upper_val
            return StockEvaluation(
                code=code, name=name, strategy_key="boll_breakout",
                selected=selected, score=score,
                indicators=indic, conditions=conditions,
                reason="布林带下轨反弹，突破中轨" if selected else "未满足布林带突破条件",
                trace_log="\n".join(trace)
            )
        except Exception as e:
            return StockEvaluation(code=code, name=name, strategy_key="boll_breakout",
                                   selected=False, score=0, reason=f"计算异常: {e}",
                                   trace_log="\n".join(trace))

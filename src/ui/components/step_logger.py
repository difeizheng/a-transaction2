"""步骤日志组件：替代 st.progress + st.empty 的覆盖式进度显示"""
import time
import streamlit as st


class StepLogger:
    """用 st.status 展示可累积的步骤日志，支持内联错误，不崩溃页面。"""

    def __init__(self, title: str, expanded: bool = True):
        self._status = st.status(title, expanded=expanded)
        self._start = time.time()
        self._step_no = 0

    def _elapsed(self) -> str:
        return f"{time.time() - self._start:.0f}s"

    def step(self, name: str):
        """开始一个命名步骤。"""
        self._step_no += 1
        with self._status:
            st.markdown(f"**步骤 {self._step_no}：{name}**")
        self._status.update(label=f"{name}…")

    def log(self, msg: str):
        """追加一行普通日志。"""
        with self._status:
            st.caption(msg)

    def progress(self, current: int, total: int, detail: str = ""):
        """在当前步骤内更新进度条。"""
        if total <= 0:
            return
        pct = min(current / total, 1.0)
        with self._status:
            st.progress(pct)
            suffix = f" | {detail}" if detail else ""
            st.caption(f"{current}/{total}{suffix} | 已用 {self._elapsed()}")

    def warning(self, msg: str):
        with self._status:
            st.warning(msg)

    def error(self, msg: str):
        with self._status:
            st.error(msg)

    def complete(self, summary: str = ""):
        label = summary or f"完成（{self._elapsed()}）"
        self._status.update(label=label, state="complete", expanded=False)

    def fail(self, summary: str = ""):
        label = summary or "失败"
        self._status.update(label=label, state="error", expanded=True)

    # ── 回调工厂 ──────────────────────────────────────────────────────────

    def make_screener_callback(self, strategy_name: str, strategy_idx: int, total_strategies: int):
        """适配 strategy.screen() 的 callback(current, total, code, name) 签名。"""
        def cb(current: int, total: int, code: str, name: str):
            if total <= 0:
                return
            elapsed = time.time() - self._start
            overall = (strategy_idx + current / total) / max(total_strategies, 1)
            label = name if name else code
            with self._status:
                st.progress(min(overall, 1.0))
                if current > 0 and elapsed > 0:
                    speed = (strategy_idx * total + current) / elapsed
                    remaining = (total_strategies - strategy_idx) * total - current
                    eta = f"预计剩余 {remaining / speed:.0f}s" if speed > 0 else ""
                else:
                    eta = "计算中…"
                st.caption(
                    f"{strategy_name} | `{code}` {label} | "
                    f"{current}/{total} | 已用 {elapsed:.0f}s | {eta}"
                )
        return cb

    def make_backtest_callback(self, strategy_options: dict, selected: list):
        """适配 BacktestComparator 的 callback(strategy_idx, total, stage, detail) 签名。"""
        STAGE_WEIGHT = {"选股": 0.2, "拉取数据": 0.5, "运行回测": 0.3, "完成": 0.0}
        start = self._start

        def cb(strategy_idx: int, total: int, stage: str, detail: str):
            elapsed = time.time() - start
            base = strategy_idx / max(total, 1)
            overall = min(base + STAGE_WEIGHT.get(stage, 0) / max(total, 1), 1.0)
            name = strategy_options.get(selected[strategy_idx] if strategy_idx < len(selected) else "", "")
            with self._status:
                st.progress(overall)
                if stage == "完成":
                    st.caption(f"回测完成，耗时 {elapsed:.1f}s")
                else:
                    st.caption(
                        f"**{name}** ({strategy_idx + 1}/{total}) | "
                        f"{stage} | {detail} | 已用 {elapsed:.0f}s"
                    )
        return cb

    def make_analysis_callback(self):
        """适配 Advisor.batch_analyze() 的 callback(current, total, code, name, stage) 签名。"""
        start = self._start

        def cb(current: int, total: int, code: str, name: str, stage: str):
            elapsed = time.time() - start
            overall = max(current - (1 if stage == "拉取新闻" else 0), 0) / max(total, 1)
            with self._status:
                st.progress(min(overall, 1.0))
                st.caption(f"{name}（{code}）| {stage} | {current}/{total} | 已用 {elapsed:.0f}s")
        return cb

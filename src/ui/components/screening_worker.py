"""后台筛选工作线程：股票优先模式——每只股票跑完所有策略后再写 SQLite，支持断点续跑。"""
import json
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

_FUNDAMENTAL_KEYS = {"low_valuation", "high_growth", "industry_leader"}
_BATCH_SIZE = 20  # 每积累多少只股票批量写一次 SQLite


def _ev_to_dict(session_id: int, strategy_key: str, ev) -> dict:
    return {
        "session_id": session_id, "strategy_key": strategy_key,
        "code": ev.code, "name": ev.name,
        "selected": 1 if ev.selected else 0, "score": ev.score,
        "indicators": json.dumps(ev.indicators, ensure_ascii=False),
        "conditions": json.dumps(
            [{"label": c.label, "passed": c.passed, "detail": c.detail} for c in ev.conditions],
            ensure_ascii=False,
        ),
        "reason": ev.reason, "trace_log": ev.trace_log,
    }


def _err_dict(session_id: int, strategy_key: str, code: str, name: str, error: str) -> dict:
    return {
        "session_id": session_id, "strategy_key": strategy_key,
        "code": code, "name": name, "selected": 0, "score": 0,
        "indicators": "{}", "conditions": "[]",
        "reason": f"评估异常: {error}", "trace_log": "",
    }


def screening_worker(
    task_state: dict,
    dm,
    screener,
    selected_strategies: list,
    strategy_options: dict,
    selected_industry,
    top_n: int,
    mode: str,
    cancel_event: threading.Event,
    session_id: int,
):
    """
    后台线程：股票优先模式。
    对每只股票依次运行所有策略，批量写 SQLite。
    通过 task_state dict 与 UI 通信，通过 SQLite 传递评分卡数据。
    """
    storage = dm.storage
    try:
        num_strategies = len(selected_strategies)
        task_state["strategies_total"] = num_strategies
        task_state["logs"].append("获取股票池…")

        pool = screener.get_stock_pool(selected_industry)
        pool_size = len(pool)
        task_state["logs"].append(f"股票池：{pool_size} 只，策略：{num_strategies} 个")
        storage.update_screening_session(session_id, {"pool_size": pool_size})

        # 预加载基本面数据（避免逐股重复请求）
        has_fundamental = any(k in _FUNDAMENTAL_KEYS for k in selected_strategies)
        if has_fundamental:
            task_state["logs"].append("预加载财务数据…")
            try:
                dm.get_latest_financial_batch(pool["code"].tolist())
            except Exception as e:
                logger.warning(f"预加载财务数据失败: {e}")

        # 实例化所有策略
        strategies = {}
        for key in selected_strategies:
            try:
                strategies[key] = screener.create_strategy(key)
            except Exception as e:
                logger.error(f"创建策略 {key} 失败: {e}")
                task_state["logs"].append(f"[错误] 创建策略 {key} 失败: {e}")

        # 断点续跑：找出已完成所有策略评估的股票
        processed = storage.get_fully_processed_codes(session_id, num_strategies)
        remaining = [(r["code"], r.get("name", "")) for _, r in pool.iterrows()
                     if r["code"] not in processed]
        total_remaining = len(remaining)
        resumed = len(processed)
        if resumed:
            task_state["logs"].append(f"断点续跑，跳过已处理 {resumed} 只，剩余 {total_remaining} 只")

        task_state["current_progress"] = resumed
        task_state["current_total"] = pool_size

        batch = []
        for i, (code, name) in enumerate(remaining):
            if cancel_event.is_set():
                if batch:
                    storage.insert_evaluations_batch(batch)
                storage.update_screening_session(session_id, {"status": "stopped"})
                task_state["status"] = "stopped"
                task_state["logs"].append("用户停止筛选")
                return

            task_state["current_stock"] = f"{code} {name}".strip()
            task_state["current_progress"] = resumed + i + 1
            # 当前策略进度：显示正在评估哪个策略
            task_state["current_strategy"] = f"{code} {name}".strip()

            # 对该股票逐一运行所有策略
            for key in selected_strategies:
                strategy = strategies.get(key)
                if strategy is None:
                    batch.append(_err_dict(session_id, key, code, name, "策略未初始化"))
                    continue
                if strategy.supports_evaluate():
                    try:
                        ev = strategy.evaluate_stock(code, name, dm)
                        batch.append(_ev_to_dict(session_id, key, ev))
                    except Exception as e:
                        logger.warning(f"{key} 评估 {code} 失败: {e}")
                        batch.append(_err_dict(session_id, key, code, name, str(e)))
                else:
                    # fallback：该策略不支持逐股评估，跳过（后续单独处理）
                    batch.append(_err_dict(session_id, key, code, name, "策略不支持逐股评估"))

            if len(batch) >= _BATCH_SIZE * num_strategies:
                storage.insert_evaluations_batch(batch)
                batch.clear()

        if batch:
            storage.insert_evaluations_batch(batch)

        # 处理不支持 evaluate_stock 的策略（如 multi_factor），用批量 screen() 补充
        fallback_keys = [k for k in selected_strategies
                         if k in strategies and not strategies[k].supports_evaluate()]
        for key in fallback_keys:
            if cancel_event.is_set():
                break
            strategy = strategies[key]
            strategy_name = strategy_options.get(key, key)
            task_state["logs"].append(f"{strategy_name} 使用批量模式…")

            def _cb(cur, total, code, name):
                task_state["current_stock"] = f"{code} {name}".strip()

            try:
                results = strategy.screen(pool, dm, progress_callback=_cb)
            except Exception as e:
                logger.error(f"策略 {strategy_name} 批量筛选失败: {e}")
                task_state["logs"].append(f"[错误] {strategy_name}: {e}")
                results = []

            selected_codes = {r.code for r in results}
            fb_batch = []
            for r in results:
                fb_batch.append({
                    "session_id": session_id, "strategy_key": key,
                    "code": r.code, "name": r.name,
                    "selected": 1, "score": r.score,
                    "indicators": json.dumps(r.signals or {}, ensure_ascii=False),
                    "conditions": "[]", "reason": r.reason, "trace_log": "",
                })
            for _, row in pool.iterrows():
                if row["code"] not in selected_codes:
                    fb_batch.append({
                        "session_id": session_id, "strategy_key": key,
                        "code": row["code"], "name": row.get("name", ""),
                        "selected": 0, "score": 0,
                        "indicators": "{}", "conditions": "[]",
                        "reason": "批量策略未入选", "trace_log": "",
                    })
            storage.insert_evaluations_batch(fb_batch)
            task_state["logs"].append(f"{strategy_name} 完成：命中 {len(results)} 只")

        if cancel_event.is_set():
            storage.update_screening_session(session_id, {"status": "stopped"})
            task_state["status"] = "stopped"
            return

        # 合并结果（应用 union / intersect 逻辑）
        from collections import defaultdict
        evals = storage.get_session_evaluations(session_id, selected_only=True)
        code_data: dict = defaultdict(lambda: {"scores": [], "strategies": [], "ev": None})
        for ev in evals:
            c = ev["code"]
            code_data[c]["scores"].append(ev["score"])
            code_data[c]["strategies"].append(strategy_options.get(ev["strategy_key"], ev["strategy_key"]))
            if code_data[c]["ev"] is None:
                code_data[c]["ev"] = ev

        if mode == "intersect":
            code_data = {k: v for k, v in code_data.items()
                         if len(v["strategies"]) == num_strategies}

        merged = []
        for c, data in code_data.items():
            ev = data["ev"]
            avg_score = round(sum(data["scores"]) / len(data["scores"]), 4)
            merged.append({**ev, "score": avg_score,
                           "reason": f"策略: {', '.join(data['strategies'])} | {ev['reason']}"})

        final = sorted(merged, key=lambda x: x["score"], reverse=True)[:top_n]
        selected_count = len(final)

        storage.update_screening_session(session_id, {
            "status": "completed",
            "finished_at": datetime.now().isoformat(),
            "processed_count": pool_size,
            "selected_count": selected_count,
        })

        task_state["final_results"] = final
        task_state["status"] = "completed"
        task_state["logs"].append(f"筛选完成，共 {selected_count} 只入选")

    except Exception as e:
        logger.error(f"筛选线程异常: {e}", exc_info=True)
        task_state["error"] = str(e)
        task_state["status"] = "failed"
        try:
            storage.update_screening_session(session_id, {"status": "failed"})
        except Exception:
            pass

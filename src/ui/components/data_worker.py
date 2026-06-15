"""数据更新后台线程：通过共享dict与UI通信，不调用任何Streamlit API。"""
import json
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def data_update_worker(
    task_state: dict,
    dm,
    update_type: str,
    scope: str,
    codes: list = None,
    rate_limit: float = 0.3,
):
    """
    在后台线程中执行数据更新。
    task_state 写入键：status, phase, current_code, current_progress,
                       current_total, logs, bars_result, financial_result, error, log_id
    """
    try:
        # 获取股票列表
        task_state["phase"] = "初始化"
        task_state["logs"].append("获取股票列表…")
        if codes is None:
            stock_df = dm.storage.get_stock_list()
            codes = stock_df["code"].tolist()
        task_state["current_total"] = len(codes)
        task_state["logs"].append(f"共 {len(codes)} 只股票")

        # 创建更新日志记录
        log_id = dm.storage.insert_update_log({
            "update_type": update_type,
            "scope": scope,
            "started_at": datetime.now().isoformat(),
            "status": "running",
            "total_count": len(codes),
        })
        task_state["log_id"] = log_id

        cancel_flag = lambda: task_state.get("status") != "running"

        def make_progress_cb(phase_name: str):
            def cb(current, total, code, msg):
                task_state["current_code"] = code
                task_state["current_progress"] = current
                task_state["current_total"] = total
                if current % 20 == 0 or "失败" in msg:
                    task_state["logs"].append(
                        f"[{phase_name}] {current}/{total} {code} {msg}"
                    )
            return cb

        bars_result = None
        financial_result = None

        if scope in ("bars", "all"):
            task_state["phase"] = "K线数据"
            task_state["logs"].append(f"开始更新K线数据（{update_type}）…")
            bars_result = dm.batch_update_bars_v2(
                codes,
                update_type=update_type,
                progress_callback=make_progress_cb("K线"),
                cancel_flag=cancel_flag,
                rate_limit=rate_limit,
            )
            task_state["bars_result"] = bars_result
            task_state["logs"].append(
                f"K线完成：成功{bars_result['success']} 跳过{bars_result['skipped']} 失败{bars_result['failed']}"
            )

        if scope in ("financial", "all") and not cancel_flag():
            task_state["phase"] = "财务数据"
            task_state["logs"].append(f"开始更新财务数据（{update_type}）…")
            fin_rate = max(rate_limit, 0.5)
            financial_result = dm.batch_update_financial_v2(
                codes,
                update_type=update_type,
                progress_callback=make_progress_cb("财务"),
                cancel_flag=cancel_flag,
                rate_limit=fin_rate,
            )
            task_state["financial_result"] = financial_result
            task_state["logs"].append(
                f"财务完成：成功{financial_result['success']} 跳过{financial_result['skipped']} 失败{financial_result['failed']}"
            )

        # 汇总失败代码
        all_failed = []
        if bars_result:
            all_failed.extend(bars_result.get("failed_codes", []))
        if financial_result:
            all_failed.extend(financial_result.get("failed_codes", []))

        final_status = "cancelled" if cancel_flag() else "completed"
        elapsed = time.time() - task_state["start_time"]

        dm.storage.update_update_log(log_id, {
            "finished_at": datetime.now().isoformat(),
            "duration_seconds": round(elapsed, 1),
            "success_count": (bars_result["success"] if bars_result else 0) +
                             (financial_result["success"] if financial_result else 0),
            "failure_count": len(all_failed),
            "failed_codes": json.dumps(all_failed[:100]),  # 最多记录100个
            "status": final_status,
        })

        task_state["status"] = final_status
        task_state["logs"].append(f"更新{final_status}，耗时 {elapsed:.0f}s")

    except Exception as e:
        logger.error(f"数据更新线程异常：{e}")
        task_state["error"] = str(e)
        task_state["status"] = "failed"
        if task_state.get("log_id"):
            try:
                dm.storage.update_update_log(task_state["log_id"], {
                    "finished_at": datetime.now().isoformat(),
                    "status": "failed",
                    "failed_codes": json.dumps([str(e)]),
                })
            except Exception:
                pass

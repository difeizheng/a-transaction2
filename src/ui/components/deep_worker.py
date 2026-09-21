"""个股深度分析后台工作线程。

为什么后台化：深度分析含 LLM 调用，单次 30~90s。同步执行时整个 rerun 期间
没有任何帧推给浏览器，websocket 会因长时间静默而死亡——客户端表现为
「步骤打勾但结果不渲染、之后所有点击失效，只能刷新」（第四轮巡检实证）。
后台线程 + 前台短 rerun 轮询（与 screening_worker 同模式）让 websocket 持续保活。

结果同时落库 deep_analysis_reports：即使客户端帧丢失，结果也不丢。
"""
import logging
import threading

logger = logging.getLogger(__name__)


def deep_analysis_worker(task_state: dict, dm, advisor, code: str, name: str):
    """后台线程：拉新闻 → 深度分析（行情+技术+LLM）→ 落库。通过 task_state 通信。"""
    try:
        task_state["step"] = "拉取个股新闻"
        try:
            dm.fetch_and_save_news(code=code)
            n = 0
            try:
                n = len(dm.get_news(code=code, limit=10))
            except Exception:
                pass
            task_state["logs"].append(f"新闻就绪（现有 {n} 条）")
        except Exception as e:
            # 新闻拉取失败不阻断主流程（新闻只是输入之一）
            task_state["logs"].append(f"新闻拉取失败（继续分析）: {e}")
            logger.warning(f"深度分析新闻拉取失败 {code}: {e}")

        task_state["step"] = "实时行情 + 技术指标 + AI综合分析（LLM 约 30~90s）"
        result = advisor.analyze_stock_deep(code, name)
        from datetime import datetime as _dt
        result["analyzed_at"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

        llm_ok = not (result.get("llm_analysis") or {}).get("llm_error", False)
        task_state["logs"].append("AI分析完成" if llm_ok else "AI分析失败（LLM 超时/不可用），已保留数据面结果")

        task_state["step"] = "保存分析报告"
        try:
            dm.storage.save_deep_analysis(code, name, result, llm_ok=llm_ok)
        except Exception as e:
            logger.warning(f"深度分析落库失败 {code}: {e}")
            task_state["logs"].append(f"报告落库失败（不影响本次展示）: {e}")

        task_state["result"] = result
        task_state["llm_ok"] = llm_ok
        task_state["status"] = "done"
    except Exception as e:
        logger.exception(f"深度分析失败 {code}")
        task_state["status"] = "error"
        task_state["error"] = str(e)


def start_deep_analysis(dm, advisor, code: str, name: str) -> dict:
    """创建 task_state 并启动后台线程，返回 task_state（调用方存 session_state）。"""
    task_state = {
        "status": "running", "step": "启动", "logs": [],
        "result": None, "llm_ok": True, "error": None,
        "code": code, "name": name,
    }
    t = threading.Thread(
        target=deep_analysis_worker, args=(task_state, dm, advisor, code, name),
        daemon=True, name=f"deep-analysis-{code}",
    )
    t.start()
    task_state["thread"] = t
    return task_state

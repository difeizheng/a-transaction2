"""数据债一次性修复脚本 —— docs/投资系统审计报告.md 的三处「代码已修、数据未修」残留。

三个阶段（可单独跑，均可断点续跑）：

  stocklist   含退市股的全量股票清单（tushare L+D+P，消除幸存者偏差，P1-2）【分钟级】
  financial   财务 ann_date 回填（tushare fina_indicator，P1-A）【小时级】
  bars        K线全量重拉（刷新陈旧 qfq 复权价，P0-e）【小时级】

用法（项目根目录）：

    python scripts/repair_data_debt.py --phase stocklist
    python scripts/repair_data_debt.py --phase financial --rate 0.5
    python scripts/repair_data_debt.py --phase bars --rate 0.3
    python scripts/repair_data_debt.py --phase all        # stocklist → financial → bars

断点续跑：完成的 code 记录在 data/repair_progress.json，中断后重跑自动跳过。
stocklist 阶段的失败会终止后续阶段（tushare token 无效时 financial 也没法跑）。
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from sqlalchemy import text  # noqa: E402

from src.config import get_config  # noqa: E402
from src.data.storage import Storage  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(_ROOT / "data" / "repair.log", encoding="utf-8")],
)
logger = logging.getLogger("repair_data_debt")

_PROGRESS_PATH = _ROOT / "data" / "repair_progress.json"


def _load_progress() -> dict:
    if _PROGRESS_PATH.exists():
        return json.loads(_PROGRESS_PATH.read_text(encoding="utf-8"))
    return {"financial": [], "bars": []}


def _save_progress(prog: dict) -> None:
    _PROGRESS_PATH.write_text(json.dumps(prog, ensure_ascii=False), encoding="utf-8")


def _get_tushare_fetcher():
    """直接构造 TushareFetcher（绕过路由——本脚本必须用 tushare 拿 ann_date/退市股）。"""
    from src.data.tushare_fetcher import TushareFetcher
    token = get_config().get("data_sources", {}).get("tushare", {}).get("token", "")
    if not token or "your" in str(token).lower():
        logger.error("未配置有效 TUSHARE_TOKEN（.env 或环境变量），stocklist/financial 阶段无法执行")
        sys.exit(2)
    return TushareFetcher(token)


def phase_stocklist(storage: Storage) -> None:
    """P1-2：拉取 L+D+P 全量股票清单（含退市股），替换 stock_list 表。"""
    ts = _get_tushare_fetcher()
    logger.info("[stocklist] 拉取含退市股的全量股票清单（L+D+P）...")
    df = ts.get_stock_list(include_delisted=True)  # 顺便验证 token 有效性
    if df.empty:
        logger.error("[stocklist] tushare 返回空——token 无效或积分不足，终止")
        sys.exit(2)
    storage.upsert_stock_list(df)
    counts = df["list_status"].value_counts().to_dict() if "list_status" in df.columns else {}
    logger.info(f"[stocklist] 完成：共 {len(df)} 只，状态分布 {counts}")


def phase_financial(storage: Storage, rate: float) -> None:
    """P1-A：为 ann_date 为 NULL 的财务记录回填披露日（tushare 直取，绕过路由）。"""
    ts = _get_tushare_fetcher()
    prog = _load_progress()
    done = set(prog["financial"])
    with storage.engine.connect() as conn:
        codes = [r[0] for r in conn.execute(
            text("SELECT DISTINCT code FROM financial_data WHERE ann_date IS NULL"))]
    todo = [c for c in codes if c not in done]
    logger.info(f"[financial] 待回填 {len(todo)} 只（总 {len(codes)}，已完成 {len(done)}）")
    for i, code in enumerate(todo):
        try:
            df = ts.get_financial_indicators(code)
            if not df.empty:
                storage.upsert_financial_data(df)
            prog["financial"].append(code)
            if (i + 1) % 20 == 0:
                _save_progress(prog)
                logger.info(f"[financial] {i + 1}/{len(todo)}（{code}）")
        except Exception as e:
            logger.warning(f"[financial] {code} 失败（下轮重试）: {e}")
        time.sleep(rate)
    _save_progress(prog)
    logger.info("[financial] 完成")


def phase_bars(storage: Storage, rate: float) -> None:
    """P0-e：K线全量重拉（update-existing 的 upsert 只对新数据生效，陈旧 qfq 价需重拉刷新）。"""
    from src.data.manager import DataManager
    prog = _load_progress()
    done = set(prog["bars"])
    with storage.engine.connect() as conn:
        codes = [r[0] for r in conn.execute(text("SELECT DISTINCT code FROM daily_bars"))]
    todo = [c for c in codes if c not in done]
    logger.info(f"[bars] 待全量重拉 {len(todo)} 只（总 {len(codes)}，已完成 {len(done)}）")

    dm = DataManager()
    result = dm.batch_update_bars_v2(todo, update_type="full", rate_limit=rate)
    prog["bars"].extend(c for c in todo if c not in set(result["failed_codes"]))
    _save_progress(prog)
    logger.info(f"[bars] 完成：{result}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True,
                    choices=["stocklist", "financial", "bars", "all"])
    ap.add_argument("--rate", type=float, default=None,
                    help="每次请求间隔秒（默认 financial=0.5, bars=0.3）")
    args = ap.parse_args()

    storage = Storage(get_config()["database"]["path"])
    phases = ["stocklist", "financial", "bars"] if args.phase == "all" else [args.phase]
    for ph in phases:
        logger.info(f"===== 阶段 {ph} 开始 =====")
        if ph == "stocklist":
            phase_stocklist(storage)
        elif ph == "financial":
            phase_financial(storage, args.rate if args.rate is not None else 0.5)
        elif ph == "bars":
            phase_bars(storage, args.rate if args.rate is not None else 0.3)
        logger.info(f"===== 阶段 {ph} 结束 =====")


if __name__ == "__main__":
    main()

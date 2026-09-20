"""数据管理页面"""
import json
import time
import threading
from datetime import date, datetime, timedelta
import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from src.data.manager import DataManager
from src.ui.components.data_worker import data_update_worker
from src.ui.components.service_info import render_service_info, get_akshare_info, get_sqlite_info


@st.cache_resource
def get_dm():
    return DataManager()


def render_sidebar():
    st.subheader("更新控制")
    dm = get_dm()

    update_type = st.radio(
        "更新方式", ["incremental", "full"],
        format_func=lambda x: "增量更新（推荐）" if x == "incremental" else "全量更新",
    )
    scope = st.radio(
        "更新范围", ["bars", "financial", "all"],
        format_func=lambda x: {"bars": "K线数据", "financial": "财务数据", "all": "全部数据"}[x],
    )
    rate_limit = st.slider("请求间隔(秒)", 0.1, 2.0, 0.3, 0.1,
                           help="AKShare调用间隔，过小可能被封IP")

    task = st.session_state.get("data_update_task", {})
    is_running = (task.get("status") == "running" and
                  task.get("thread") and task["thread"].is_alive())

    if is_running:
        if st.button("停止更新", type="secondary", use_container_width=True):
            task["status"] = "cancelled"
    else:
        if st.button("开始更新", type="primary", use_container_width=True):
            task_state = {
                "status": "running",
                "thread": None,
                "phase": "初始化",
                "current_code": "",
                "current_progress": 0,
                "current_total": 0,
                "start_time": time.time(),
                "logs": [],
                "bars_result": None,
                "financial_result": None,
                "error": None,
                "log_id": None,
            }
            t = threading.Thread(
                target=data_update_worker,
                args=(task_state, dm, update_type, scope, None, rate_limit),
                daemon=True,
            )
            task_state["thread"] = t
            st.session_state["data_update_task"] = task_state
            t.start()

    # 收盘提醒
    now = datetime.now()
    # 修复：原条件 `hour >= 15 and minute >= 30` 要求分钟也 ≥30，
    # 导致 16:00、17:00 等整点（minute=0）不提示。改为 15:30 之后持续提示。
    if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
        logs = dm.storage.get_update_logs(limit=1)
        today_str = date.today().isoformat()
        if logs.empty or (not logs.empty and str(logs.iloc[0]["started_at"])[:10] < today_str):
            st.info("市场已收盘，建议执行今日数据更新")

    st.divider()
    render_service_info([get_akshare_info(), get_sqlite_info()])


def _render_progress(task: dict):
    """更新运行中时在页面顶部显示进度，sleep+rerun轮询。"""
    if task.get("status") == "running":
        thread = task.get("thread")
        if thread and not thread.is_alive():
            task["status"] = "failed"
            task["error"] = "更新线程意外终止"

    status = task.get("status", "idle")

    if status == "running":
        elapsed = time.time() - task.get("start_time", time.time())
        cur = task.get("current_progress", 0)
        total = task.get("current_total", 1)
        phase = task.get("phase", "")
        code = task.get("current_code", "")
        pct = cur / max(total, 1)

        with st.status(f"更新中：{phase}（{cur}/{total}）", expanded=True):
            for line in task.get("logs", [])[-8:]:
                st.caption(line)
            st.progress(pct)
            if cur > 0 and elapsed > 0:
                speed = cur / elapsed
                eta = (total - cur) / speed if speed > 0 else 0
                st.caption(f"`{code}` | 已用 {elapsed:.0f}s | 预计剩余 {eta:.0f}s")

        time.sleep(1)
        st.rerun()

    elif status in ("completed", "cancelled"):
        # 更新会改变覆盖率/过期名单，清掉概览缓存让新数据立刻可见
        _cached_overview.clear()
        _cached_stale.clear()
        _load_coverage.clear()
        elapsed = time.time() - task.get("start_time", time.time())
        label = "更新完成" if status == "completed" else "已停止"
        st.success(f"{label}，耗时 {elapsed:.0f}s")
        bars = task.get("bars_result")
        fin = task.get("financial_result")
        if bars:
            no_data = bars.get('no_data', 0)
            line = f"K线：成功 {bars['success']} | 跳过 {bars['skipped']} | 无数据(停牌等) {no_data} | 失败 {bars['failed']}"
            st.caption(line)
        if fin:
            st.caption(f"财务：成功 {fin['success']} | 跳过 {fin['skipped']} | 失败 {fin['failed']}")

    elif status == "failed":
        st.error(f"更新失败：{task.get('error', '未知错误')}")


@st.cache_data(ttl=120, show_spinner=False)
def _cached_overview():
    """概览 COUNT 查询结果缓存 120s——更新进度轮询每秒 rerun 一次，
    不缓存等于每秒对 561MB 库做一遍多表 COUNT，是 websocket 假死的诱因。"""
    return get_dm().storage.get_data_overview()


@st.cache_data(ttl=120, show_spinner=False)
def _cached_stale(threshold: str):
    return get_dm().storage.get_stale_stocks(threshold)


def _render_overview(dm):
    ov = _cached_overview()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("总股票数", f"{ov['total_stocks']:,}")
    c2.metric("K线覆盖率", f"{ov['bars_coverage_pct']}%",
              f"{ov['stocks_with_bars']:,} 只")
    c3.metric("财务覆盖率", f"{ov['financial_coverage_pct']}%",
              f"{ov['stocks_with_financial']:,} 只")
    c4.metric("数据库大小", f"{ov['db_size_bytes'] / 1024 / 1024:.1f} MB")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("K线总条数", f"{ov['total_bars']:,}")
    c6.metric("最新K线日期", ov["latest_bar_date"] or "无")
    c7.metric("财务总条数", f"{ov['total_financial']:,}")
    fin_latest = (ov["latest_financial_date"] or "")[:10]
    c8.metric("最新财务报告期", fin_latest or "无")

    # 财务新鲜度按季报口径判断：最新报告期应 ≥ 上一个已结束季度的季末，
    # 否则才是真正陈旧（K 线按日、财务按季，不能用同一把尺子）
    if fin_latest:
        today = date.today()
        prev_q_end_month = (today.month - 1) // 3 * 3
        if prev_q_end_month == 0:
            expected = date(today.year - 1, 12, 31).isoformat()
        else:
            _q_end_day = {3: 31, 6: 30, 9: 30, 12: 31}[prev_q_end_month]
            expected = date(today.year, prev_q_end_month, _q_end_day).isoformat()
        if fin_latest < expected:
            st.warning(
                f"⚠️ 财务数据最新报告期 {fin_latest}，落后于上季末 {expected}，"
                "建议运行财务数据更新")
        else:
            st.caption(f"财务报告期 {fin_latest} 为当前最新（季报口径，披露有滞后属正常）")

    # 过期数据提示
    threshold = (date.today() - timedelta(days=5)).isoformat()
    stale = _cached_stale(threshold)
    if not stale.empty:
        col_warn, col_btn = st.columns([3, 1])
        col_warn.warning(f"⚠️ {len(stale)} 只股票的K线数据超过5个交易日未更新（或无数据）")
        if col_btn.button("更新过期股票K线", type="primary", use_container_width=True):
            task = st.session_state.get("data_update_task", {})
            is_running = (task.get("status") == "running" and
                          task.get("thread") and task["thread"].is_alive())
            if is_running:
                st.toast("已有更新任务在运行，请等待完成后再试", icon="⚠️")
            else:
                stale_codes = stale["code"].tolist()
                task_state = {
                    "status": "running", "thread": None,
                    "phase": "初始化", "current_code": "",
                    "current_progress": 0, "current_total": 0,
                    "start_time": time.time(), "logs": [],
                    "bars_result": None, "financial_result": None,
                    "error": None, "log_id": None,
                }
                t = threading.Thread(
                    target=data_update_worker,
                    args=(task_state, dm, "incremental", "bars", stale_codes, 0.3),
                    daemon=True,
                )
                task_state["thread"] = t
                st.session_state["data_update_task"] = task_state
                t.start()
                st.rerun()


@st.cache_data(ttl=60)
def _load_coverage(_dm_id):
    dm = get_dm()
    bars = dm.storage.get_bars_coverage()
    fin = dm.storage.get_financial_coverage()
    merged = bars.merge(fin[["code", "fin_count", "latest_report"]], on="code", how="left")
    return merged


def _render_stock_table(dm):
    st.caption("数据每60秒自动刷新，或点击右上角刷新按钮")
    df = _load_coverage(id(dm))

    col1, col2 = st.columns([2, 1])
    search = col1.text_input("搜索代码/名称", placeholder="如 000001 或 平安")
    status_filter = col2.selectbox("数据状态", ["全部", "有K线", "缺K线", "缺财务", "数据过期"])

    if search:
        df = df[df["code"].str.contains(search) | df["name"].str.contains(search, na=False)]

    threshold = (date.today() - timedelta(days=5)).isoformat()
    if status_filter == "有K线":
        df = df[df["bar_count"] > 0]
    elif status_filter == "缺K线":
        df = df[df["bar_count"] == 0]
    elif status_filter == "缺财务":
        df = df[df["fin_count"].isna() | (df["fin_count"] == 0)]
    elif status_filter == "数据过期":
        df = df[df["last_date"].isna() | (df["last_date"] < threshold)]

    display = df[["code", "name", "market", "first_date", "last_date", "bar_count", "fin_count", "latest_report"]].copy()
    display.columns = ["代码", "名称", "市场", "K线起始", "K线最新", "K线条数", "财务条数", "最新财务"]
    st.dataframe(display, use_container_width=True, hide_index=True, height=400)
    st.caption(f"显示 {len(display)} / {len(df)} 只股票")


def _render_update_log(dm):
    logs = dm.storage.get_update_logs(limit=50)
    if logs.empty:
        st.info("暂无更新记录")
        return

    display_cols = [c for c in ["started_at", "update_type", "scope", "duration_seconds",
                                 "success_count", "failure_count", "status"] if c in logs.columns]
    rename = {
        "started_at": "开始时间", "update_type": "类型", "scope": "范围",
        "duration_seconds": "耗时(秒)", "success_count": "成功", "failure_count": "失败", "status": "状态",
    }
    st.dataframe(logs[display_cols].rename(columns=rename),
                 use_container_width=True, hide_index=True)

    # 失败详情
    failed_logs = logs[logs["failure_count"].fillna(0) > 0]
    if not failed_logs.empty:
        with st.expander(f"失败详情（{len(failed_logs)} 条记录有失败）"):
            for _, row in failed_logs.iterrows():
                codes_str = row.get("failed_codes", "[]")
                try:
                    codes = json.loads(codes_str) if codes_str else []
                except Exception:
                    codes = []
                if codes:
                    st.caption(f"{row['started_at']} — 失败股票：{', '.join(codes[:20])}"
                               + ("…" if len(codes) > 20 else ""))


def _render_stock_detail(dm):
    code_input = st.text_input("输入股票代码（多只用逗号分隔）", placeholder="如 000001 或 000001,600036")
    if not code_input:
        st.info("输入股票代码查看详情")
        return

    codes_list = [c.strip() for c in code_input.split(",") if c.strip()]
    code = codes_list[0]  # 详情展示用第一只

    detail = dm.storage.get_stock_detail(code)
    info = detail.get("info", {})
    if not info:
        st.warning(f"未找到股票 {code}，请确认代码正确")
        return

    label = f"{info.get('name', code)}（{code}）"
    if len(codes_list) > 1:
        label += f" 等 {len(codes_list)} 只"
    st.subheader(label)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("K线条数", detail["bar_count"])
    c2.metric("K线日期范围", f"{detail['first_date'] or '无'} ~ {detail['last_date'] or '无'}")
    c3.metric("财务条数", detail["fin_count"])
    c4.metric("相关新闻", detail["news_count"])

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("强制刷新数据", type="primary"):
            with st.spinner("重新拉取中…"):
                result = dm.force_refresh_stock(code)
            if result["success"]:
                st.success(f"刷新完成：K线 {result['bars_count']} 条，财务 {result['financial_count']} 条")
                st.cache_data.clear()
            else:
                st.error(f"刷新失败：{result['error']}")

    with col_btn2:
        if st.button("删除本地数据", type="secondary"):
            dm.storage.delete_stock_data(code)
            st.success(f"已删除 {code} 的K线和财务数据")
            st.cache_data.clear()

    # 指定时间段更新K线
    st.divider()
    st.caption("指定时间段更新K线（强制从数据源拉取，不受本地已有数据影响）")
    col_d1, col_d2, col_d3 = st.columns([2, 2, 1])
    range_start = col_d1.date_input("开始日期", value=date.today() - timedelta(days=30), key="range_start")
    range_end = col_d2.date_input("结束日期", value=date.today(), key="range_end")
    if col_d3.button("更新K线", type="primary", use_container_width=True):
        if range_start > range_end:
            st.error("开始日期不能晚于结束日期")
        else:
            start_str = range_start.isoformat()
            end_str = range_end.isoformat()
            results = []
            with st.spinner(f"正在更新 {len(codes_list)} 只股票 {start_str} ~ {end_str} 的K线…"):
                for c in codes_list:
                    try:
                        df = dm.fetch_bars_range(c, start_str, end_str)
                        results.append(f"{c}: {len(df)} 条")
                    except Exception as e:
                        results.append(f"{c}: 失败（{e}）")
            st.success("更新完成：" + "，".join(results))
            st.cache_data.clear()

    # 数据详情
    tab_bars, tab_fin, tab_news = st.tabs(["K线数据", "财务数据", "新闻"])

    with tab_bars:
        bars_df = dm.storage.get_daily_bars(code)
        if bars_df.empty:
            st.info("暂无K线数据")
        else:
            st.dataframe(bars_df.tail(30), use_container_width=True, hide_index=True)
            fig = go.Figure(go.Candlestick(
                x=bars_df["trade_date"].tail(60),
                open=bars_df["open"].tail(60),
                high=bars_df["high"].tail(60),
                low=bars_df["low"].tail(60),
                close=bars_df["close"].tail(60),
            ))
            fig.update_layout(title=f"{code} 近60日K线", height=300, xaxis_rangeslider_visible=False)
            st.plotly_chart(fig, use_container_width=True)

    with tab_fin:
        fin_df = dm.storage.get_financial_data(code)
        if fin_df.empty:
            st.info("暂无财务数据")
        else:
            st.dataframe(fin_df, use_container_width=True, hide_index=True)

    with tab_news:
        news_df = dm.get_news(code=code, limit=20)
        if news_df.empty:
            st.info("暂无相关新闻")
        else:
            show_cols = [c for c in ["publish_time", "title", "source"] if c in news_df.columns]
            st.dataframe(news_df[show_cols].rename(columns={
                "publish_time": "时间", "title": "标题", "source": "来源"
            }), use_container_width=True, hide_index=True)


def _render_source_management(dm):
    """Tab5：数据源管理"""
    # 数据源元信息
    SOURCE_META = {
        "akshare": {
            "label": "AKShare",
            "desc": "开源免费，无需账号，覆盖K线/财务/行情/新闻",
            "url": "https://akshare.akfamily.xyz",
            "needs_token": False,
        },
        "tushare": {
            "label": "Tushare Pro",
            "desc": "专业数据，需要token（2000分以上），K线/财务/公告质量高",
            "url": "https://tushare.pro",
            "needs_token": True,
            "token_key": "data_sources.tushare.token",
        },
        "tencent": {
            "label": "腾讯财经",
            "desc": "免费实时行情，无需账号，适合盘中实时价格查询",
            "url": "https://finance.qq.com",
            "needs_token": False,
        },
        "cninfo": {
            "label": "巨潮资讯",
            "desc": "官方公告数据，免费无需账号，作为公告备源",
            "url": "http://www.cninfo.com.cn",
            "needs_token": False,
        },
        "ifind": {
            "label": "同花顺 iFinD",
            "desc": "专业机构数据，需要token，暂未接入（预留）",
            "url": "https://www.ifind.com.cn",
            "needs_token": True,
            "reserved": True,
        },
    }

    available_sources = dm.router.get_available_sources()

    # ── 数据源卡片 ──
    st.subheader("数据源状态")
    status_list = dm.storage.get_source_status()
    status_map = {(s["source_name"], s["data_type"]): s for s in status_list}

    cols = st.columns(len(SOURCE_META))
    for col, (src_name, meta) in zip(cols, SOURCE_META.items()):
        with col:
            is_available = src_name in available_sources
            is_reserved = meta.get("reserved", False)
            if is_reserved:
                indicator = "⚪"
                status_text = "预留"
            elif is_available:
                # 检查是否有近期失败
                has_failure = any(
                    v["failure_count"] > 0
                    for (sn, _), v in status_map.items()
                    if sn == src_name
                )
                indicator = "🟡" if has_failure else "🟢"
                status_text = "有失败记录" if has_failure else "正常"
            else:
                indicator = "🔴"
                status_text = "未启用"

            st.markdown(f"**{indicator} {meta['label']}**")
            st.caption(f"{status_text} · [{meta['url'].split('//')[1].split('/')[0]}]({meta['url']})")
            st.caption(meta["desc"])

    st.divider()

    # ── 路由配置表 ──
    st.subheader("路由配置")
    st.caption("为每种数据类型指定主源和备源，主源失败时自动切换到备源")

    routes = dm.router.get_routes()
    DATA_TYPE_LABELS = {
        "stock_list":      "股票列表",
        "daily_bars":      "K线数据",
        "financial":       "财务数据",
        "realtime":        "实时行情",
        "industry_list":   "行业列表",
        "industry_stocks": "行业成分股",
        "announcements":   "公告数据",
        "news":            "新闻/舆情",
    }
    source_options = available_sources + ["（无）"]

    route_changes = {}
    for route in routes:
        dt = route["data_type"]
        label = DATA_TYPE_LABELS.get(dt, dt)
        c1, c2, c3 = st.columns([2, 2, 2])
        with c1:
            st.markdown(f"**{label}**")
        with c2:
            cur_primary = route.get("primary_source", "akshare")
            opts_p = [s for s in available_sources]
            idx_p = opts_p.index(cur_primary) if cur_primary in opts_p else 0
            new_primary = st.selectbox(
                "主源", opts_p, index=idx_p,
                key=f"route_primary_{dt}", label_visibility="collapsed"
            )
        with c3:
            cur_backup = route.get("backup_source") or "（无）"
            opts_b = source_options
            idx_b = opts_b.index(cur_backup) if cur_backup in opts_b else len(opts_b) - 1
            new_backup = st.selectbox(
                "备源", opts_b, index=idx_b,
                key=f"route_backup_{dt}", label_visibility="collapsed"
            )
        if new_primary != cur_primary or new_backup != (route.get("backup_source") or "（无）"):
            route_changes[dt] = {
                "primary": new_primary,
                "backup": None if new_backup == "（无）" else new_backup,
            }

    if st.button("保存路由配置", type="primary"):
        for dt, change in route_changes.items():
            dm.router.set_route(dt, change["primary"], change["backup"])
        dm.router.reload_routes()
        st.success(f"已保存 {len(route_changes)} 项路由配置")
        st.rerun()

    st.divider()

    # ── 健康状态面板 ──
    st.subheader("健康状态")
    if not status_list:
        st.info("暂无调用记录，开始数据更新后会自动统计")
    else:
        status_df = pd.DataFrame(status_list)
        status_df["source_label"] = status_df["source_name"].map(
            {k: v["label"] for k, v in SOURCE_META.items()}
        ).fillna(status_df["source_name"])
        status_df["data_type_label"] = status_df["data_type"].map(DATA_TYPE_LABELS).fillna(status_df["data_type"])
        display = status_df[[
            "source_label", "data_type_label",
            "success_count", "failure_count",
            "last_success_at", "last_failure_at", "last_error"
        ]].rename(columns={
            "source_label": "数据源", "data_type_label": "数据类型",
            "success_count": "成功次数", "failure_count": "失败次数",
            "last_success_at": "最后成功", "last_failure_at": "最后失败", "last_error": "最后错误",
        })
        st.dataframe(display, use_container_width=True, hide_index=True)

        if st.button("重置统计数据", type="secondary"):
            dm.storage.reset_source_status()
            st.success("统计数据已重置")
            st.rerun()

    st.divider()

    # ── 连接测试 ──
    st.subheader("连接测试")
    st.caption("测试各数据源是否可正常访问（拉取少量数据验证）")

    test_cols = st.columns(len([s for s in available_sources]))
    for col, src_name in zip(test_cols, available_sources):
        meta = SOURCE_META.get(src_name, {"label": src_name})
        with col:
            if st.button(f"测试 {meta['label']}", key=f"test_{src_name}"):
                fetcher = dm.router.fetchers.get(src_name)
                if fetcher is None:
                    st.error("未初始化")
                else:
                    with st.spinner("测试中…"):
                        try:
                            if src_name == "tencent":
                                result = fetcher.get_realtime_quotes(["000001"])
                            elif src_name == "cninfo":
                                result = fetcher.get_stock_news("000001")
                            else:
                                result = fetcher.get_stock_list()
                            if result is not None and not result.empty:
                                st.success(f"✓ 返回 {len(result)} 条")
                            else:
                                st.warning("返回空数据")
                        except NotImplementedError:
                            st.info("该源不支持此测试接口")
                        except Exception as e:
                            st.error(f"失败: {e}")


def render():
    st.title("🗄️ 数据管理")
    dm = get_dm()

    # 更新进度（顶部）
    task = st.session_state.get("data_update_task")
    if task and task.get("status") in ("running", "completed", "cancelled", "failed"):
        _render_progress(task)
        if task.get("status") != "running":
            st.divider()

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["数据概览", "股票数据状态", "更新日志", "个股管理", "数据源管理"])
    with tab1:
        _render_overview(dm)
    with tab2:
        _render_stock_table(dm)
    with tab3:
        _render_update_log(dm)
    with tab4:
        _render_stock_detail(dm)
    with tab5:
        _render_source_management(dm)

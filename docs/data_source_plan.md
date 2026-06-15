# 数据源管理模块实施计划

> 创建日期：2026-04-06  
> 状态：待实施（Step 1 起）

## 背景

当前系统仅依赖 AKShare 单一数据源，存在以下问题：
- AKShare 批量更新 5000+ 股票时频繁出现 `RemoteDisconnected` 错误
- 用户已有 Tushare（2000+ 积分）和同花顺 iFinD token 未被利用
- 实时行情应优先用免费的腾讯财经，节省 Tushare 积分
- 单源故障时整个更新流程中断，无自动切换能力

## 数据源分配

| 数据类型 | 主源 | 备源 |
|---------|------|------|
| K线数据 | Tushare | AKShare |
| 财务数据 | Tushare | AKShare |
| 实时行情 | 腾讯财经 | AKShare |
| 股票列表 | Tushare | AKShare |
| 行业分类 | AKShare | Tushare |
| 公告数据 | Tushare | 巨潮资讯 |
| 新闻/舆情 | AKShare | Tushare |
| iFinD | 预留 | — |
| 网页爬虫 | 预留（路径B） | — |

**配置策略：** 敏感 token 存 `config/config.yaml`，路由状态（主/备/失败计数）存 SQLite。

---

## 分步实施计划

### ✅ 已完成
- 数据管理页面（4个Tab：概览/状态/日志/个股管理）
- 后台线程更新（断点续跑、进度回调、取消支持）
- update_log 表、统计查询方法

---

### Step 1：BaseFetcher 抽象层
**文件：** `src/data/base_fetcher.py`（新建），`src/data/fetcher.py`（修改）

新建抽象基类：
```python
from abc import ABC, abstractmethod
import pandas as pd

class BaseFetcher(ABC):
    name: str

    @abstractmethod
    def get_stock_list(self) -> pd.DataFrame:
        """返回列: code, name, market"""

    @abstractmethod
    def get_daily_bars(self, code, start_date, end_date, adjust="qfq") -> pd.DataFrame:
        """返回列: code, trade_date, open, high, low, close, volume, amount, pct_chg"""

    @abstractmethod
    def get_financial_indicators(self, code) -> pd.DataFrame:
        """返回列: code, report_date, pe_ttm, pb, roe"""

    @abstractmethod
    def get_realtime_quotes(self, codes) -> pd.DataFrame:
        """返回列: code, name, price, pct_chg, volume, amount"""

    @abstractmethod
    def get_industry_list(self) -> pd.DataFrame:

    @abstractmethod
    def get_industry_stocks(self, industry) -> pd.DataFrame:

    @abstractmethod
    def get_stock_news(self, code) -> pd.DataFrame:
        """返回列: title, content, publish_time, source, related_codes"""

    @abstractmethod
    def get_news_em(self, pages=3) -> pd.DataFrame:
```

修改 `AKShareFetcher`：继承 `BaseFetcher`，添加 `name = "akshare"`。

**验证：** 启动服务，数据管理页面功能不变。

---

### Step 2：Tushare 数据源
**文件：** `src/data/tushare_fetcher.py`（新建），`config/config.yaml`（修改）

config.yaml 新增：
```yaml
data_sources:
  tushare:
    token: "你的token"
    enabled: true
  tencent:
    enabled: true
  cninfo:
    enabled: true
  ifind:
    token: ""
    enabled: false
```

TushareFetcher 实现：
- `get_stock_list()`：`pro.stock_basic()` → 标准化 code/name/market
- `get_daily_bars()`：`ts.pro_bar()` → 标准化列名，trade_date 转 date 类型
- `get_financial_indicators()`：`pro.fina_indicator()` → 标准化 pe_ttm/pb/roe
- `get_realtime_quotes()`：`raise NotImplementedError`（不消耗积分）
- 其他方法：按需实现或 raise NotImplementedError

**注意：** Tushare 日期格式为 `YYYYMMDD`，需与系统 `YYYY-MM-DD` 互转。

**验证：** Python 控制台 `TushareFetcher(token).get_stock_list()` 返回标准 DataFrame。

---

### Step 3：腾讯财经实时行情
**文件：** `src/data/tencent_fetcher.py`（新建）

接口：`http://qt.gtimg.cn/q=sh000001,sz000002,...`

返回格式解析：
```
v_sh000001="1~上证指数~000001~3300.00~..."
```
字段位置固定，按索引提取 name/price/pct_chg/volume/amount。

批量请求：每次最多 100 只，多批次合并。

**验证：** `TencentFetcher().get_realtime_quotes(["000001","600519"])` 返回标准格式。

---

### Step 4：巨潮资讯公告源
**文件：** `src/data/cninfo_fetcher.py`（新建）

接口：`http://www.cninfo.com.cn/new/hisAnnouncement/query`（POST）

仅实现 `get_stock_news(code)`，其他方法 `raise NotImplementedError`。

**验证：** `CninfoFetcher().get_stock_news("000001")` 返回标准新闻格式。

---

### Step 5：路由层 + SQLite 表
**文件：** `src/data/source_router.py`（新建），`src/data/storage.py`（修改）

storage.py 新增两张表：
```sql
CREATE TABLE IF NOT EXISTS data_source_routes (
    data_type TEXT PRIMARY KEY,
    primary_source TEXT NOT NULL,
    backup_source TEXT,
    enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS data_source_status (
    source_name TEXT,
    data_type TEXT,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT,
    PRIMARY KEY (source_name, data_type)
);
```

storage.py 新增方法：
- `get_source_routes() -> list[dict]`
- `upsert_source_route(data_type, primary, backup)`
- `get_source_status() -> list[dict]`
- `record_source_success(source, data_type)`
- `record_source_failure(source, data_type, error)`
- `reset_source_status(source=None, data_type=None)`
- `init_default_routes()`：按分配表写入默认路由

SourceRouter 核心逻辑：
```python
def call(self, data_type: str, method: str, *args, **kwargs) -> pd.DataFrame:
    primary = self.routes[data_type]["primary"]
    backup = self.routes[data_type]["backup"]
    
    # 尝试主源
    try:
        result = getattr(self.fetchers[primary], method)(*args, **kwargs)
        self.storage.record_source_success(primary, data_type)
        return result
    except (NotImplementedError, KeyError):
        raise  # 不是网络错误，不切换
    except Exception as e:
        self.storage.record_source_failure(primary, data_type, str(e))
        logger.warning(f"{data_type} 主源 {primary} 失败: {e}，切换到 {backup}")
    
    # 尝试备源
    try:
        result = getattr(self.fetchers[backup], method)(*args, **kwargs)
        self.storage.record_source_success(backup, data_type)
        return result
    except Exception as e:
        self.storage.record_source_failure(backup, data_type, str(e))
        raise RuntimeError(f"{data_type} 主备源均失败") from e
```

**验证：** 单元测试：mock 主源抛异常，验证自动切换到备源。

---

### Step 6：DataManager 接入路由层
**文件：** `src/data/manager.py`（修改）

初始化改造：
```python
def __init__(self):
    cfg = get_config()
    self.storage = Storage(cfg["database"]["path"])
    
    fetchers = {"akshare": AKShareFetcher()}
    ds_cfg = cfg.get("data_sources", {})
    if ds_cfg.get("tushare", {}).get("enabled") and ds_cfg["tushare"].get("token"):
        from src.data.tushare_fetcher import TushareFetcher
        fetchers["tushare"] = TushareFetcher(ds_cfg["tushare"]["token"])
    if ds_cfg.get("tencent", {}).get("enabled"):
        from src.data.tencent_fetcher import TencentFetcher
        fetchers["tencent"] = TencentFetcher()
    if ds_cfg.get("cninfo", {}).get("enabled"):
        from src.data.cninfo_fetcher import CninfoFetcher
        fetchers["cninfo"] = CninfoFetcher()
    
    routes = self.storage.get_source_routes()
    self.router = SourceRouter(self.storage, fetchers, routes)
    self.fetcher = fetchers["akshare"]  # 兼容性保留
```

所有数据获取调用替换：
```python
# 之前
df = self.fetcher.get_daily_bars(code, start, end)
# 之后
df = self.router.call("daily_bars", "get_daily_bars", code, start, end)
```

**验证：** 数据管理页面更新功能通过路由层正常工作，日志显示使用的数据源。

---

### Step 7：UI — 数据源管理 Tab
**文件：** `src/ui/page_modules/data_mgmt.py`（修改）

在现有4个Tab后新增 Tab5 "数据源管理"：

```
Tab5 数据源管理
├── 数据源卡片（每个源一张）
│   ├── 源名称 + 状态指示灯（绿=正常/红=有失败/灰=未启用）
│   ├── 说明（地址/特点）
│   ├── Token 输入框（Tushare/iFinD）+ 掩码显示
│   └── 启用/禁用开关
├── 路由配置表
│   ├── 数据类型 | 主源（selectbox）| 备源（selectbox）| 启用
│   └── 保存按钮
├── 健康状态面板
│   ├── 各源各类型的成功/失败计数、最后成功/失败时间
│   └── 重置统计按钮
└── 连接测试
    └── 每个源一个"测试连接"按钮（拉取少量数据验证）
```

**验证：** UI 可查看路由配置、切换主备源、测试连接、查看健康状态。

---

### Step 8（预留）：iFinD 接入
需要用户提供 iFinDPy 的具体接入方式（接口文档或示例代码）。

### Step 9（预留）：网页爬虫框架
路径B扩展，按需实现。

---

## 关键文件清单

| 文件 | 操作 | 所属步骤 |
|------|------|---------|
| `src/data/base_fetcher.py` | 新建 | Step 1 |
| `src/data/fetcher.py` | 修改（继承） | Step 1 |
| `src/data/tushare_fetcher.py` | 新建 | Step 2 |
| `config/config.yaml` | 修改（新增data_sources段） | Step 2 |
| `src/data/tencent_fetcher.py` | 新建 | Step 3 |
| `src/data/cninfo_fetcher.py` | 新建 | Step 4 |
| `src/data/source_router.py` | 新建 | Step 5 |
| `src/data/storage.py` | 修改（新增2表+6方法） | Step 5 |
| `src/data/manager.py` | 修改（接入路由层） | Step 6 |
| `src/ui/page_modules/data_mgmt.py` | 修改（新增Tab5） | Step 7 |

---

## 注意事项

1. **Tushare 积分**：K线和财务数据消耗积分，实时行情不用 Tushare
2. **腾讯财经**：免费但非官方 API，接口可能变动，需做好异常处理
3. **巨潮资讯**：POST 接口，需要正确的 headers 模拟浏览器请求
4. **AKShare 兼容**：所有改动保持向后兼容，AKShare 始终作为最终备源
5. **路由状态**：每次任务开始时重新尝试主源（不跨任务持久化切换状态）

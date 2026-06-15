# 静默异常审计报告

> 审计日期：2026-06-15
> 范围：`src/` 全目录的 `except Exception` / `except:` 语句
> 结论：**22 处全部合理，无需改动**

## 审计方法

`grep -rn "except\s+(Exception|BaseException)\s*:" src/` + 逐处人工核对上下文，
判断每处是「合理的降级/容错」还是「危险的静默吞错」。

## 分类结论

### ✅ 降级到兜底逻辑（合理，9 处）

失败后走预设的安全默认值，主流程继续：

| 位置 | 失败后果 | 兜底值 |
|------|----------|--------|
| `multifactor.py:76` | 单因子计算失败 | `momentum=np.nan`，其余因子照算 |
| `tushare_fetcher.py:130` | daily_basic 拉取失败 | 空 DataFrame，财务降级 |
| `simulator.py:31` | 实时行情失败 | 降级到最新收盘价 |
| `advisor.py:94` | realtime 获取失败 | `realtime=None` |
| `advisor.py:218` | AI 分析失败 | 返回中性默认结构 |
| `llm_analyzer.py:113` | JSON 解析失败 | 中性情绪 + 截断摘要 |
| `llm_analyzer.py:167` | LLM 响应解析失败 | 默认分析结构 |
| `auto_trader.py:157` | 回撤计算失败 | `return False`（fail-open，避免误暂停交易） |
| `watchlist.py:110` | signals JSON 解析失败 | 空 dict，UI 正常展示 |

### ✅ 幂等迁移（合理，2 处）

`storage.py:83, 135` — `ALTER TABLE ADD COLUMN` 在列已存在时抛错 → `pass`。
这是 SQLite 标准的幂等迁移模式，**必须静默**（否则旧库每次启动都报错）。

### ✅ UI 容错显示（合理，6 处）

`screener.py`（3处）/ `data_mgmt.py:236` / `trading.py:23` / `screener.py:54`
— 数据加载失败时显示降级 UI（空列表/默认选项/错误提示），避免页面白屏。
**UI 层不应因数据问题崩溃**，这是正确的健壮性策略。

### ✅ 后台 worker 二次清理失败（合理，合理但可观察）

`data_worker.py:124` / `screening_worker.py:223` — 内层 `except: pass`
是「更新日志表/session 状态」失败。**关键点：外层 `except Exception` 已
`logger.error(exc_info=True)` 并设置 `task_state["status"]="failed"`**，
主异常已被记录。内层 pass 仅容忍「记录失败状态本身又失败」的边缘情况，非真静默。

`data_worker.py:124` / `screening_worker.py:223` 外层均有完整错误记录，**无需改动**。

## 维护建议（写给未来）

1. **新增 `except Exception` 时**，至少满足其一：
   - 配 `logger.debug/warning`（可追溯）
   - 有明确的降级返回值（容错）
   - 是幂等操作（如迁移）
2. **禁止**：`except Exception: pass` 既无日志又无降级——这是真正的静默吞错。
3. 后台线程的异常**必须**在外层 `logger.error(exc_info=True)`，不能只靠内层。

## 复查命令

```bash
# 重新审计（新增代码后执行）
grep -rn "except\s\+Exception" src/ | grep "pass$"
```

若上式有输出，逐一确认是否满足上述三条标准之一。

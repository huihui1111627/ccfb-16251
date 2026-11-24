# 参数敏感计划缓存实验室

一个只用 Python 标准库实现的教学型数据库内核 Demo：

- SQL 词法/语法解析、字面量规范化；
- MCV + 等深直方图和选择率估算；
- 基于代价的优化器：索引扫描 vs 全表扫描、Nested Loop vs Hash Join；
- 真实执行器：索引回表、顺序扫描、连接、排序、聚合、主键点查；
- 按规范化 SQL 键缓存，并识别参数敏感性、按选择率分档缓存；
- 全局容量上限、LRU 淘汰、手动锁定计划/禁止缓存；
- 统计信息更新后按依赖表失效缓存；
- 幂律商户数据、混合流量、重启和统计更新事件；
- 三种策略对照：**分档缓存**、**每语句只缓存一个计划**、**每次重新优化**；
- 原生 HTML/CSS/JS 可视化页面，无前端构建和第三方运行时依赖。

## 快速开始

要求 Python 3.9+。

```bash
# 方式一：模块入口
python3 -m psdb web --calls 720 --cache-entries 5 --port 8766

# 方式二：便捷入口（可用 PORT 环境变量覆盖端口）
python3 app.py
```

服务会立即监听端口；首次打开页面时后台仍在运行基准，完成后页面自动刷新：

```text
http://127.0.0.1:8766/
```

也可以只跑命令行基准并生成 JSON：

```bash
python3 -m psdb benchmark --calls 720 --cache-entries 5 \
  --out benchmark-report.json
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 推荐观察路径

1. 顶部 KPI：商户订单量差约 **3 万倍**，单计划缓存的 P95 明显恶化。
2. “逐次调用耗时轨迹”：
   - 红线是只缓存一个计划；
   - 蓝线是每次重新优化；
   - 绿线是参数敏感分档缓存；
   - 橙色虚线是重启，绿色虚线是统计信息更新。
3. “每条语句的选择率分档与计划”：
   - 主查询有三档：
     - 小选择率：`index_nl`，索引扫描 + Nested Loop；
     - 中选择率：`index_hash`，索引扫描 + Hash Join；
     - 大选择率：`seq_hash`，全表扫描 + Hash Join。
   - 每张档位卡显示选择率区间、估算行数、成本、执行计划和缓存状态。
   - 下方的直方图探测点说明是哪些 MCV/Histogram bucket 得到了不同最优计划。
4. “最近调用落在哪个档”：显示每次调用的商户选择率、档位、计划和缓存状态。
5. “计划缓存 / 淘汰 / 手动干预”：
   - 拖动缓存上限并应用，可立即看到 LRU 淘汰；
   - 每条语句右上角可恢复自动分档、强制指定某个计划或禁止缓存；
   - 顶部可模拟重启、重新采集统计、给指定商户插入爆单后更新统计。

## 手动 API

页面按钮本质上调用以下 JSON API：

```bash
# 以商户 700 发起订单查询
curl -X POST http://127.0.0.1:8766/api/call \
  -H 'Content-Type: application/json' \
  -d '{"kind":"main","merchant_id":700}'

# 强制某条语句始终使用全表扫描 + Hash Join
curl -X POST http://127.0.0.1:8766/api/policy \
  -H 'Content-Type: application/json' \
  -d '{"statement_id":"stmt_xxxxxxxx","mode":"pinned","forced_signature":"seq_hash"}'

# 禁止某条语句使用缓存
curl -X POST http://127.0.0.1:8766/api/policy \
  -H 'Content-Type: application/json' \
  -d '{"statement_id":"stmt_xxxxxxxx","mode":"disabled"}'

# 修改缓存容量并执行 LRU 淘汰
curl -X POST http://127.0.0.1:8766/api/cache-limit \
  -H 'Content-Type: application/json' \
  -d '{"max_entries":4}'

# 重启清空计划缓存
curl -X POST http://127.0.0.1:8766/api/restart

# 给商户 120 插入 5000 单，再收集统计信息并失效相关计划
curl -X POST http://127.0.0.1:8766/api/refresh-stats \
  -H 'Content-Type: application/json' \
  -d '{"merchant_id":120,"burst_size":5000}'
```

完整页面状态：

```bash
curl http://127.0.0.1:8766/api/state
```

## 实现结构

| 文件 | 职责 |
|---|---|
| `psdb/sql.py` | Tokenizer、递归下降 parser、AST、字面量 `?` 规范化 |
| `psdb/stats.py` | 表统计、MCV、等深直方图、选择率估算、统计采集 |
| `psdb/optimizer.py` | 代价模型、候选计划、参数敏感性探测、选择率分档和交叉边界 |
| `psdb/database.py` | 内存表、主键/二级索引、幂律商户订单生成、爆单写入 |
| `psdb/executor.py` | 索引扫描、全表扫描、回表、NL/Hash Join、排序、聚合 |
| `psdb/cache.py` | 规范化 SQL + tier 键、LRU、容量、淘汰、失效、手动策略 |
| `psdb/engine.py` | 串联 parse/estimate/optimize/cache/execute，并记录逐次耗时 |
| `psdb/workload.py` | 请求模板、流量权重、脚本化大小商户和事件时间线 |
| `psdb/benchmark.py` | 三策略同负载对照、分位数和汇总指标 |
| `psdb/dashboard.py` | 标准库 HTTP 服务和 JSON API |
| `psdb/web/` | 仪表盘页面、样式和 Canvas 图表 |
| `tests/test_psdb.py` | 解析、分档、缓存、淘汰、策略、失效和小型基准测试 |

## 缓存键和分档

规范化文本例如：

```text
select o.id, ... from orders o inner join merchants m ...
where o.merchant_id = ? order by o.created_at desc limit ?
```

自动模式的实际缓存键是：

```text
<normalized-sql>||t1
<normalized-sql>||t2
<normalized-sql>||t3
```

新参数到来时：

1. 用直方图计算 `merchant_id = ?` 的选择率；
2. 在已生成的选择率区间中找到 tier；
3. 用 `(normalized_sql, tier_id)` 查 LRU；
4. 未命中时才重新优化并放入该档计划。

敏感性判断会遍历直方图 MCV 和 bucket representative values。如果不同直方图区间得到不同 winner，就把语句标记为参数敏感。分档边界通过对数选择率扫描和二分交叉点细化得到。

## 代价和耗时口径

页面同时展示两类耗时：

- **模型耗时**：代价常数乘以执行器实际处理的扫描行、回表行、连接行和排序行数。它能稳定反映错误计划导致的算法量级差异。
- **实测墙钟**：Python 进程内 parser、optimizer 和 executor 的 `perf_counter()` 耗时。因为数据在内存且没有真实磁盘随机 I/O，绝对数值不等于生产数据库，但计划方向和分布差异可复现。

优化器累计耗时是真实 Python 执行时间。分档模式第一次遇到语句或统计更新后会做直方图 what-if 探测，因此完整建档次数很少，但单次建档比普通优化重；稳定状态下每次只做选择率计算、查档和 LRU 访问。

## 一次本地基准的典型现象

默认 720 次请求、缓存上限 5 时，一次运行中可能看到：

- 分档缓存的最优计划匹配率为 100%；
- 单计划缓存只有约 55%～60% 的调用选中当时最优计划；
- 单计划缓存 P95 / 均值显著高于分档缓存；
- reopt 每次都能选对计划，但完整优化次数为 720；
- 分档缓存完整优化/重建次数通常只有几十次；
- 缓存上限小于总档位数时会出现 LRU 淘汰和偶发 `TIER_REBUILD`；把上限调到 6 通常可容纳当前三条语句的全部 6 个档/点查条目。

具体数字会随机器负载和 Python 计时轻微变化。

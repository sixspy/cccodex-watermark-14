# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印）

## 缺口补盖（watermark-14）

补盖范围只能二选一，**写库范围与缺口列表共用同一取数口径**（同一批
“应盖印批次”行集）：

| 范围 | 对缺口行 | 对已盖印行 |
|------|----------|------------|
| `仅缺口` | 写入新印 | 原样保留（水印与盖印时间都不动） |
| `整批重落` | 写入新印 | 先整笔回滚整批印，再全量重写（水印被替换） |

- 点补盖 → `POST /api/backfills {as_of_date, scope}`，一次落库；失败整笔回滚，
  返回白话原因（`{"ok": false, "error": "..."}`）。
- 补盖详情 `GET /api/backfills/{id}`：亮 **范围芯片**（`scope_chip`）与命运标记；
  回放 `POST /api/backfills/{id}/replay {rounds: 2}` 逐轮落库，每轮带
  `scope_chip`、`fate_chips`（新盖/保留/替换/跳过）和 `aligned`，两轮数字与芯片可对齐核对。
- 缺口列表 `GET /api/gaps?as_of_date=...`：`expected_count` / `stamped_count` /
  `gap_count` 与 `rows`（`is_gap` 标记）同源。
- 引擎在 `BEGIN IMMEDIATE` 排他事务里“读缺口 → 写补盖 → 复核”；补盖前另抢一行
  全局任务锁（`backfill_locks`），两名仓管并发点补盖时后到者收到
  “另一名仓管正在补盖，请稍后再试。”，不会双写同一行。
- 自检：`仅缺口` 下已盖印行被误替、或补盖后缺口不为 0、或写库范围与缺口口径
  对不上，都会抛错并整笔回滚。

预留切口（未做，供后续功能题）：模板换版生效窗、盖印作废回灌、跨表依赖冻结、
追索深度限制等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`

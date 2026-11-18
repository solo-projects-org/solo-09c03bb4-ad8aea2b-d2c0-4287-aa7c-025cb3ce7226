# Satellite Scene Catalog API

目录服务：遥感数据员登记卫星景的 **B04（红光）/ B08（近红外）** 两个波段文件，
农情分析组按 **地块经纬矩形 + UTC 时段** 查询现行景，并下载已验真的波段文件。

技术栈：FastAPI + SQLite（WAL）+ 本地内容寻址文件存储 + Rasterio（空间质控），
Docker Compose 一键启动，自带 Swagger 入口。

## 1. 启动

```bash
cd catalog
docker compose up --build -d
```

- Swagger UI：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>
- 数据持久化在命名卷 `catalog-data`（SQLite 库 + 波段文件，容器重启不丢）

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/scenes/{scene_id}/revisions` | multipart 表单提交一个修订（元数据 + B04/B08 两文件 + 各自 SHA-256） |
| GET | `/scenes` | 按 bbox（`west,south,east,north`）与 `start_time/end_time`（UTC ISO-8601）查询，只返回每个景的**现行修订**；已开启空间质控的景仅当现行修订**质控通过**时才返回 |
| GET | `/scenes/{scene_id}/revisions/{revision}` | 取任意历史修订（含被取代的旧修订）及其下载地址 |
| GET | `/scenes/{scene_id}/revisions/{revision}/bands/{band}/download` | 下载 B04/B08，响应头带 `X-SHA-256` |
| POST | `/scenes/{scene_id}/qc` | 对**现行修订**启动空间质控（后台执行，`202` 立即返回）；普通非 GeoTIFF 文件拒绝开启（`422`） |
| GET | `/scenes/{scene_id}/qc` | 查询现行修订的质控状态与不符原因 |
| GET | `/health` | 存活/数据库检查 |

### 提交示例

```bash
curl -X POST http://localhost:8000/scenes/S2A_MSIL1C_001/revisions \
  -F "revision=1" \
  -F "acquisition_time=2026-06-01T10:00:00Z" \
  -F "west=116.0" -F "south=39.0" -F "east=117.0" -F "north=40.0" \
  -F "sha256_b04=$(sha256sum b04.tif | cut -d' ' -f1)" \
  -F "sha256_b08=$(sha256sum b08.tif | cut -d' ' -f1)" \
  -F "file_b04=@b04.tif" \
  -F "file_b08=@b08.tif"
```

### 查询示例

```bash
curl "http://localhost:8000/scenes?west=116.2&south=39.2&east=116.8&north=39.8&start_time=2026-06-01T00:00:00Z&end_time=2026-06-30T23:59:59Z"
```

### 空间质控示例

```bash
# 对现行修订启动质控（202 立即返回，后台检查）
curl -X POST http://localhost:8000/scenes/S2A_MSIL1C_001/qc
# 轮询 verdict：pending（待检）→ approved（通过）/ rejected（不符，附原因）
curl http://localhost:8000/scenes/S2A_MSIL1C_001/qc
```

## 3. 空间质控（spatial QC）规则

目的：核实**登记的覆盖矩形**与实际波段影像是否一致，避免地块检索把错位影像
交给分析组。

1. **检查内容**（Rasterio 读取现行修订的 B04/B08，仅支持 **EPSG:4326、北向上**
   的单波段 GeoTIFF）：
   - 两波段均为可读的**单波段 GeoTIFF**；
   - 两波段 CRS 均为 EPSG:4326，格网**北向上**（无旋转/剪切、像元高度为负）；
   - **两波段格网一致**（行列数、地理变换参数，容差 1e-6 度）；
   - **登记矩形与影像边界一致**（west/south/east/north 四边逐一比对，容差 1e-6 度）。
   任一不符即 `rejected`，`reasons` 列出全部不符原因；全部通过即 `approved`。
2. **门控**：某景**首次启动质控后**，`/scenes` 仅当该景**现行修订质控通过**时才
   列出它；待检（pending）或不符（rejected）的修订不供检索。旧修订的下载地址
   不受影响，始终可用。从未启动质控的景沿用原有查询/下载行为。
3. **新修订转回待检**：已门控的景提交新修订时，新修订自动置为 `pending`，
   须重新通过质控才会重新出现在 `/scenes`；同修订的幂等重放不改变质控状态。
4. **异步执行**：检查在后台线程池运行，耗时检查期间其他景的提交、查询、下载
   均不受阻塞；`POST /qc` 返回 `202` 后用 `GET /qc` 轮询结果。
5. **失败语义**：结果按 `(scene_id, revision)` 落库——进程中断或结果入库失败时
   该修订保持 `pending`；旧修订的迟到结果只更新旧修订自己的行，**不会批准新版**。
6. **普通文件拒绝开启**：现行修订的波段文件不是 GeoTIFF 时，`POST /qc` 返回
   `422` 且不开启质控（不激活门控，景保持原有行为）。

## 4. 一致性规则（如何满足需求）

1. **双波段验真才可查/可下**：上传先流式落临时盘并实时计算 SHA-256，
   与申报值逐字节比对；任一不符返回 `400` 且不写任何目录记录。只有两份文件
   都通过后才在同一事务里写入 scene + 两条 band 记录。
2. **同修订重复提交幂等**：`(scene_id, revision)` 为主键。内容完全一致的重复提交
   返回 `200` + `submission_status=idempotent_replay`，响应与首次完全相同；
   内容（元数据或任一摘要）冲突返回 `409`，原记录不受影响。
3. **迟到旧修订不能盖新版**：提交时校验 `revision > 该景当前最大修订号`，
   否则 `409 stale revision`。修订号允许跳号。
4. **新版改变时空范围后查询只返回相交的现行景**：`GET /scenes` 用
   `MAX(revision)` 关联每个景的现行修订，再做矩形相交
   （`west<=east and east>=west ...`）与 UTC 时间窗过滤；旧修订不会回流。
5. **已开始的旧波段下载完整可用**：波段文件按 SHA-256 内容寻址、不可变、
   永不删除，旧修订的下载地址长期有效，新修订发布不影响进行中的下载。
6. **校验失败 / 并发 / 重启不会让目录指向缺失文件**：
   - 文件先 `os.replace` 原子落位，数据库行后插入；失败时临时文件立即清理；
   - 写事务使用 `BEGIN IMMEDIATE` + WAL + `busy_timeout`，并发提交被序列化，
     相同内容并发提交只有一个 `201`，其余幂等 `200`，冲突内容一个成功一个 `409`；
   - 启动时执行目录完整性检查（每条 band 记录的文件必须存在），缺失则拒绝启动；
     同时清理崩溃遗留的 `tmp/upload-*.part`。

## 4. 本地测试（无 Docker 时）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt httpx pytest
pytest            # 32 个端到端用例（含并发、重启、下载交错、空间质控）
uvicorn app.main:app --port 8000
```

测试覆盖：双波段验真、校验失败无残留、幂等回放、冲突拒绝、迟到修订拒绝、
时空过滤与现行修订、新旧修订下载交错、4 路并发相同/冲突提交、重启持久化、
缺失文件拒绝启动、临时文件启动清理；空间质控的通过与各类不符（矩形错位、
格网不一致、非 EPSG:4326、非北向上、多波段）、普通文件拒绝开启、待检门控、
新修订转回待检、旧修订迟到结果不批准新版、慢检查期间其他景可提交、
结果入库失败仍待检、待检状态跨重启保留。

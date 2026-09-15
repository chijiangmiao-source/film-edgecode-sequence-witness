# 胶片边码组装服务（Film Edge-Code Assembly）

电影资料馆的胶片边码扫描片段常有重复，局部重叠也会在分叉后重新汇合。本服务把片段多重集组装成完整边码串，并严格区分三种结论：**唯一卷序**、**两条不同的卷序见证**、**明确的不可组装图证据**——修复师不会把"找到一条拼法"误当成卷序唯一。

技术栈：Python 3.13 · FastAPI · Pydantic v2 · Docker Compose · pytest。

## 输入模型

`POST /assemble` 与 `POST /verify` 接收同一组输入：

| 字段 | 约束 |
| --- | --- |
| `k` | **JSON 整数**，`3 ≤ k ≤ 12`；每条片段的长度恒为 `k`。字符串 `"3"`、浮点 `3.0`、布尔值等一律按类型错误拒绝，不做隐式转换 |
| `start` | 指定起始前缀，长度恰为 `k−1`，仅含 ASCII 大写字母 `A–Z` 或数字 `0–9` |
| `fragments` | 片段多重集：每条长度恰为 `k`、仅含 `A–Z0–9`；总数 `≤ 20000`；**相同文本按出现次数保留**（重复不去重） |

图模型：以每个片段的前 `k−1` 字符与后 `k−1` 字符为顶点构造**有向多重图**——片段 `f` 是边 `f[:k-1] → f[1:]`，边标号为 `f[-1]`。完整边码串 = 起始前缀 + 依次经过的每条边的末字符，长度为 `(k−1) + n`（`n` 为片段数）。组装即求一条从 `start` 出发、每条边恰好经过一次的**欧拉迹**。

## 判定逻辑

### 可行性（`impossible` 的三类结构化图证据）

1. **度数（`degree`）**：欧拉迹要求所有顶点出入度平衡，或仅允许一个顶点 `出−入=1`（必为起点）、一个顶点 `入−出=1`（必为终点）。证据列出全部失衡顶点的 `in_degree / out_degree / balance`。
2. **起点（`start`）**：起始前缀没有任何出边（图中不存在该前缀），或度数平衡强制欧拉迹必须从另一个顶点出发。证据给出 `given_start` 与 `required_start`。
3. **连通性（`connectivity`）**：所有非零度顶点必须弱连通。证据给出连通分量数及每个分量的顶点数、边数、采样顶点。

### 唯一性证明（不枚举排列）

可行性通过后，服务用**有序 Hierholzer 算法**分别构造 ASCII 序**最小**与**最大**的完整边码串：每步走当前最小（大）剩余出边，回溯出栈逆序即欧拉迹。由交换论证，这两个极值覆盖所有合法拼法的字典序区间：

- **最小串 == 最大串** ⟺ 所有拼法产生同一字符串 ⟺ 返回 `unique`；
- **最小串 ≠ 最大串** ⟹ 两条极值串本身就是两条不同的合法卷序，作为 `ambiguous` 的见证返回，并附 `first_difference`（首个不同字符的位置与两串各自字符），且可通过 `POST /verify` 逐字符复算。

平行同文片段：边的标号由端点唯一决定（`u → u[1:]+c` 的标号必为 `c`），因此互换相同片段的拷贝产生同一字符串，**天然不计入歧义**。全程 `O(E log E)`（`E ≤ 20000`），无排列枚举、无固定响应、无占位实现。

## API

### `POST /assemble`

**唯一** `200`：

```json
{
  "status": "unique",
  "assembly": "ABCAB",
  "length": 5,
  "fragment_count": 3
}
```

**歧义** `200`（请求 `{"k":3,"start":"AB","fragments":["ABA","BAB","ABC","BCA","CAB"]}`）：

```json
{
  "status": "ambiguous",
  "witnesses": {"min": "ABABCAB", "max": "ABCABAB"},
  "first_difference": {"index": 2, "min": "A", "max": "C"},
  "fragment_count": 5
}
```

**不可组装** `200`（三类证据之一，示例为连通性）：

```json
{
  "status": "impossible",
  "reason": "connectivity",
  "evidence": {
    "kind": "connectivity",
    "component_count": 2,
    "components": [
      {"vertex_count": 3, "edge_count": 3, "sample_vertices": ["AB", "BC", "CA"]},
      {"vertex_count": 3, "edge_count": 3, "sample_vertices": ["DE", "EF", "FD"]}
    ],
    "note": "all vertices with non-zero degree must lie in a single weakly connected component"
  }
}
```

`degree` 证据含 `imbalanced_vertices`（各顶点 `in_degree/out_degree/balance`）；`start` 证据含 `given_start/required_start/start_in_degree/start_out_degree`。

### `POST /verify`

逐字符复算任一候选完整串（见证可独立验证）。请求在组装输入外增加 `candidate` 字段。返回 `{"valid": true, "candidate_length": …, "fragment_count": …}`，或 `{"valid": false, "reason": …, "detail": …}`——`reason ∈ {length_mismatch, invalid_character, start_prefix_mismatch, fragment_unavailable}`，`detail` 指出首个出错位置。

### `GET /health`

返回 `{"status": "ok"}`（Compose 健康检查使用）。

### 错误信封

所有 4xx/5xx 响应统一为结构化信封：

```json
{"error": {"code": "INVALID_FRAGMENTS", "message": "every fragment must have length k and use only ASCII A-Z / 0-9", "details": {"problems": [{"index": 3, "fragment": "abC", "problem": "characters outside A-Z0-9", "invalid_characters": ["a", "b"]}], "truncated": false}}}
```

`code` 取值：`VALIDATION_ERROR`（模式校验：k 越界、片段超 20000、字段缺失/多余等）、`INVALID_START`、`INVALID_FRAGMENTS`（语义校验：长度/字符集，含出错下标）、`NOT_FOUND`、`METHOD_NOT_ALLOWED`、`INTERNAL_ERROR`。

交互式文档见 `/docs`（OpenAPI：`/openapi.json`）。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build api            # 默认宿主端口 8000
API_PORT=9000 docker compose up --build api   # API_PORT 覆盖宿主端口
```

一次性验收服务 `verify`（等待 `api` 健康后，先跑 pytest，再对运行中的 API 做 HTTP 验收，含两万片段用例；全部通过以 0 退出）：

```bash
docker compose up --build --exit-code-from verify --abort-on-container-exit verify
docker compose down
```

### 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
pytest                                   # 算法 + 接口测试
python scripts/acceptance.py             # 对本地运行中的 API 做验收（API_BASE_URL 可覆盖）
```

## 测试

`pytest` 共 400+ 用例：

- `tests/test_assembly.py`——算法单测（重复边、汇合分叉、回路、断图、自环、数字字符、`k=12`、空多重集）；**350 个随机小实例与暴力枚举全部拼法交叉验证**（测试可以枚举，实现不枚举）；2 万片段的唯一/歧义规模用例。
- `tests/test_api.py`——接口测试：三种结论的响应结构、见证复算、各类 422 结构化错误、404/405 信封、5000 片段 HTTP 端到端。

## 项目结构

```
app/
  assembly.py     # 核心：建图、可行性证据、最小/最大欧拉串、候选串复算
  models.py       # Pydantic 请求/响应模型（判别联合）
  validation.py   # 依赖 k 的语义校验（起点/片段）
  errors.py       # 结构化错误信封与异常处理
  main.py         # FastAPI 应用与路由
scripts/
  acceptance.py   # verify 服务的一次性验收脚本
tests/            # pytest（算法 + 接口）
Dockerfile        # python:3.13-slim
docker-compose.yml
requirements.txt
pytest.ini
```

# Local PEP 440 Dependency Resolver

一个完全本地化、确定性的依赖版本解析服务。使用 **Python + FastAPI + packaging**
实现，依据请求中自带的包版本清单（无需 PyPI、无需网络、无需账号）解析出全部
传递依赖。

## 特性

- **PEP 440 版本语义**：由 [`packaging`](https://packaging.pypa.io/) 提供版本号
  解析、排序与 `>=`、`==`、`<`、`~=`、`!=` 等说明符匹配；兼容 epoch、post、dev
  等版本形式。
- **传递依赖解析**：递归处理每个选定版本声明的依赖，支持依赖图中的环。
- **锁定优先**：`locks` 中给出的版本只要仍满足所有约束，就会被优先保留；
  锁定版本被更深层依赖证伪时，才按规则回溯到其他版本，返回结果里会说明每个锁的
  处置（`applied` / `backtracked` / `unsatisfiable` / `missing-version` /
  `not-found` / `unused`）。
- **确定性回溯**：其余候选严格按版本**从高到低**尝试；候选顺序、依赖处理顺序、
  根需求顺序均为确定顺序，相同输入永远得到相同输出（含尝试次数）。
- **默认排除预发布版**：`a/b/rc/dev` 等预发布默认不参与；可通过
  `allow_prereleases=true` 全局放开，或由显式预发布钉版（如 `==2.0rc1`）、
  对该包的锁自动放开。
- **可操作的冲突报告**：无解时返回 409，并给出：
  - 冲突包名与原因（`unsatisfiable` / `not-found` / `no-versions`）；
  - 互相矛盾的约束（说明符、提出者及其版本）；
  - 每条约束完整的**依赖路径**（从根一路到提出约束的包）；
  - 候选版本清单与已尝试版本，以及整棵失败分支树，而不是一句笼统错误。
- **PEP 508**：依赖可写成 `name[extra] spec; marker` 字符串；环境标记在固定的
  默认环境下求值（可用 `marker_env` 覆盖），保证结果可复现。
- **同步 + 异步两种用法**：`POST /resolve` 立即返回；`POST /jobs` 把同样的请求
  持久化到 SQLite 后由后台 worker 顺序执行，可查询、可协作取消，重启自动恢复。
- **漏洞审计**：`POST /audit/vulnerabilities` 直接接收 `/resolve` 成功响应中的
  `resolved` 与 `dependency_graph`，对照本地公告列表（PEP 440 受影响范围）匹配
  已解析版本；每个命中项给出从 `<root>` 到该包的**全部最短路径**（支持环与菱形
  依赖，路径去重并按节点序列稳定排序），不可达命中单列为 `orphaned`。
- **许可证策略审计**：`POST /audit/licenses` 接收 `resolved` 与一份包名 → SPDX
  表达式映射，按本地 `allowed` / `denied` 标识符集合判定每个包的合规状态
  （`violation` / `compliant` / `unknown` / `missing`）。表达式由成熟的
  [`license-expression`](https://github.com/aboutcode-org/license-expression)
  库解析，支持括号、`AND`、`OR`、`WITH`（`WITH` 按 `AND` 分别判定许可证与例外）；
  响应给出规范化表达式与决定状态的标识符，任一表达式非法即整体返回 400。
- **重建计划**：`POST /plan/rebuild` 接收 `/resolve` 成功响应中的 `resolved`、
  `dependency_graph` 与本地变更包名列表，先沿反向依赖边传播得到全部受影响包，
  再把环依赖压缩为必须同批构建的组，按“依赖先于依赖者”输出可并行的 stages；
  每个受影响包还给出距最近 changed 包的最短反向路径，未安装的 changed 项单列。

## 一条命令启动

需要 Docker（含 Compose v2）。在项目根目录执行：

```bash
docker compose up --build
```

服务监听 **http://localhost:8000**：

- `GET /health` — 健康检查
- `POST /resolve` — 同步解析依赖（保持原有行为不变）
- `POST /audit/vulnerabilities` — 用本地公告列表审计已解析的依赖图
- `POST /audit/licenses` — 用本地许可证策略审计已解析的包
- `POST /plan/rebuild` — 为本地变更包生成确定性的重建计划
- `POST /jobs` — 提交异步解析作业（立即返回 `202` 与作业 ID）
- `GET /jobs/{id}` — 查询作业状态与最终响应
- `DELETE /jobs/{id}` — 取消作业
- `GET /docs` — 交互式 OpenAPI 文档（Swagger UI）

镜像在构建时安装固定版本依赖；运行时不访问任何外部仓库。

## 异步作业 API

对于回溯很深、耗时较长的解析，可以用与 `/resolve` **完全相同的请求体**提交
一个持久化作业，由内置 worker 在后台顺序执行（FIFO）。状态保存在 SQLite 中，
服务重启、容器重建后作业与结果仍然保留（见[数据持久化](#数据持久化)）。

### 提交作业

```bash
curl -s -X POST localhost:8000/jobs -H 'Content-Type: application/json' -d '{
  "packages": { "x": {"1.0": {}, "2.0": {}} },
  "root": { "x": ">=1.0" }
}'
```

返回 `202 Accepted`，并通过 `Location: /jobs/{id}` 给出查询地址：

```json
{
  "id": "59150780924745849a94bbe17e0d5155",
  "status": "queued",
  "created_at": "2026-09-17T08:00:00.000000+00:00",
  "started_at": null,
  "finished_at": null
}
```

入队前会做与 `/resolve` 一致的语法/语义校验：非法请求直接返回 `400/422`，
不会产生注定失败的作业。

### 查询作业

```bash
curl -s localhost:8000/jobs/59150780924745849a94bbe17e0d5155
```

状态机：`queued → running → succeeded | failed | cancelled`。

- `queued` / `running`：返回时间戳；running 时若已请求取消，还会带
  `"cancel_requested": true` 与当前已求值的候选次数 `attempts`。
- `succeeded`：`response` 即同步 `/resolve` 的完整响应体，`response_status`
  为其原本的 HTTP 状态码（解析成功 `200`；无解/触顶为 `409`，与同步接口一致）。
- `failed`：作业执行出现异常，`error` 给出错误码与信息。
- `cancelled`：已取消（不包含 `response`，终态不可更改）。

### 取消作业

```bash
curl -s -X DELETE localhost:8000/jobs/{id}
```

- **queued**：立即转为 `cancelled`，返回 `200`；
- **running**：worker 在**下一个候选求值边界**协作式停止，作业随即变为
  `cancelled`（不会在回溯分支中途停在不一致状态），返回 `202`；
- **cancelled**：幂等，再次取消返回 `200`；
- **succeeded / failed**：终态受保护，返回 `409`，原结果不会被覆盖。

#### 取消与完成的线性化语义

对 running 作业，`DELETE` 与 worker 的写回由两条 `BEGIN IMMEDIATE` 事务按
**提交顺序**线性化，取消优先级完全在 `JobStore` 的单次终结事务内裁决，作业外
不做任何预检决策，因此不存在“检查时未取消、提交时已取消”的窗口：

- **取消先提交**（作业仍为 running 时 `cancel_requested=1` 已落盘）：即使解析器
  刚通过最后一次候选边界取消检查、worker 随后仍提交了成功结果或失败结果，终结
  事务也会在同一事务里把终态原子地写为 `cancelled`，并把 `response`、
  `response_status`、`error` 三列一并置空——已取消的作业永远观察不到任何结果或
  错误；`attempts`（已求值候选数）仍可保留。
- **完成先提交**：作业已为 `succeeded`/`failed` 后，后续 `DELETE` 返回 `409`，
  结果原样保留（即上文既有行为）。

两种交错顺序都有确定性并发测试覆盖：用线程事件把一方固定在“对方已提交之后”再
继续，不依赖 sleep 或时序运气。

### 崩溃恢复

worker 以单条 `BEGIN IMMEDIATE` 事务原子领取最早的 `queued` 作业（按入队顺序，
FIFO），因此即使将来扩容为多个 worker 也不会重复领取同一作业。每次启动时，
上一进程遗留的 `running` 作业会被退回 `queued` 重新排队；若遗留作业此前已收到
取消请求，则直接终结为 `cancelled`。

## 漏洞审计 API

`POST /audit/vulnerabilities` 接收 `/resolve` **成功响应**中的 `resolved` 与
`dependency_graph`，外加一份本地公告列表，按已解析版本匹配受影响的包：

```bash
curl -s -X POST localhost:8000/audit/vulnerabilities \
  -H 'Content-Type: application/json' -d '{
  "resolved": {"app": "1.0", "db": "1.0", "web": "2.0", "common": "1.5"},
  "dependency_graph": [
    {"source": "<root>", "source_version": "", "target": "app", "target_version": "1.0"},
    {"source": "app", "source_version": "1.0", "target": "db", "target_version": "1.0"},
    {"source": "app", "source_version": "1.0", "target": "web", "target_version": "2.0"},
    {"source": "db", "source_version": "1.0", "target": "common", "target_version": "1.5"},
    {"source": "web", "source_version": "2.0", "target": "common", "target_version": "1.5"}
  ],
  "advisories": [
    {"id": "CVE-2026-0001", "package": "common", "affected": ">=1.0,<1.6"}
  ]
}'
```

请求字段：

- `resolved`：包名 → 已解析版本（PEP 440），即 `/resolve` 的 `resolved`。
- `dependency_graph`：边列表，即 `/resolve` 的 `dependency_graph`；每条边含
  `source` / `source_version` / `target` / `target_version`（`<root>` 边的
  `source_version` 为空字符串）。
- `advisories`：本地公告列表，每条含 `id`、`package`、`affected`
  （受影响版本的 PEP 440 说明符，如 `>=1.0,<1.6`；空字符串表示全部版本）。

响应（200）：

```json
{
  "status": "ok",
  "summary": {"advisories": 1, "matches": 1, "orphaned": 0},
  "matches": [
    {
      "advisory": {"id": "CVE-2026-0001", "package": "common", "affected": ">=1.0,<1.6"},
      "package": "common",
      "version": "1.5",
      "paths": [
        [
          {"name": "<root>", "version": ""},
          {"name": "app", "version": "1.0"},
          {"name": "db", "version": "1.0"},
          {"name": "common", "version": "1.5"}
        ],
        [
          {"name": "<root>", "version": ""},
          {"name": "app", "version": "1.0"},
          {"name": "web", "version": "2.0"},
          {"name": "common", "version": "1.5"}
        ]
      ]
    }
  ],
  "orphaned": []
}
```

- `matches`：每条命中一项，含公告原文、命中包与版本，以及从 `<root>` 到该包的
  **全部最短路径**（菱形依赖会给出多条；路径去重并按节点序列稳定排序，相同输入
  永远得到相同输出）。图允许含环——路径枚举只在 BFS 层数递增的边上前进，绝不
  因环无限遍历。
- `orphaned`：版本命中但无法从 `<root>` 到达的包（无 `paths` 字段），单列以便
  人工核查。
- 版本匹配与求解器口径一致：按 PEP 440 说明符判定，已解析的预发布版本同样参与
  匹配。

## 许可证审计 API

`POST /audit/licenses` 接收 `/resolve` **成功响应**中的 `resolved`、一份
包名 → SPDX 许可证表达式映射，以及本地策略（`allowed` / `denied` 标识符集合），
对每个已解析包判定合规状态：

```bash
curl -s -X POST localhost:8000/audit/licenses \
  -H 'Content-Type: application/json' -d '{
  "resolved": {"app": "1.0", "web": "2.0", "db": "1.5", "util": "0.9"},
  "licenses": {
    "app": "MIT",
    "web": "(Apache-2.0 OR GPL-3.0-only) AND BSD-3-Clause",
    "db": "GPL-2.0-only WITH Classpath-exception-2.0"
  },
  "allowed": ["MIT", "Apache-2.0", "BSD-3-Clause", "Classpath-exception-2.0"],
  "denied": ["GPL-3.0-only"]
}'
```

请求字段：

- `resolved`：包名 → 已解析版本（PEP 440），即 `/resolve` 的 `resolved`。
- `licenses`：包名 → SPDX 许可证表达式（支持括号、`AND`、`OR`、`WITH`，大小写
  不敏感的已知 SPDX 标识符会规范化为标准写法）。未出现在 `resolved` 中的条目
  会被忽略（映射可以是超集，例如一份许可证知识库）；`resolved` 中没有条目的包
  记为 `missing`。
- `allowed` / `denied`：可选，SPDX 标识符列表（单项标识符，不能是表达式）。
  同一标识符同时出现在两个集合中时，`denied` 优先。

判定规则：

- 单个标识符：在 `denied` 中为 `violation`，在 `allowed` 中为 `compliant`，
  否则为 `unknown`。
- `AND`：任一操作数违规即 `violation`；否则任一未知即 `unknown`；否则
  `compliant`。
- `OR`：任一操作数合规即 `compliant`；否则任一未知即 `unknown`；否则
  `violation`。
- `WITH`：按 `AND` 处理，许可证与例外作为两个标识符分别判定。

响应（200）：

```json
{
  "status": "ok",
  "summary": {"packages": 4, "violation": 0, "compliant": 2, "unknown": 1, "missing": 1},
  "results": [
    {"name": "app", "version": "1.0", "status": "compliant",
     "expression": "MIT", "decisive": ["MIT"]},
    {"name": "db", "version": "1.5", "status": "unknown",
     "expression": "GPL-2.0-only WITH Classpath-exception-2.0",
     "decisive": ["GPL-2.0-only"]},
    {"name": "util", "version": "0.9", "status": "missing",
     "expression": null, "decisive": []},
    {"name": "web", "version": "2.0", "status": "compliant",
     "expression": "(Apache-2.0 OR GPL-3.0-only) AND BSD-3-Clause",
     "decisive": ["Apache-2.0", "BSD-3-Clause"]}
  ],
  "violation": [],
  "compliant": ["app", "web"],
  "unknown": ["db"],
  "missing": ["util"]
}
```

- `results`：每个已解析包一项，含规范化后的表达式与 `decisive`——**决定该状态
  的标识符**（如违规背后的 denied 标识符、使 `OR` 合规的 allowed 标识符）。
- `violation` / `compliant` / `unknown` / `missing`：按状态分组的包名清单。
- 所有清单与 `results` 均按规范化包名稳定排序，相同输入永远得到相同输出。

校验规则（违反即返回 `400`，错误体与 `/resolve` 相同，且不产生任何部分结果）：

- `resolved` 中的版本必须是合法 PEP 440 版本（与漏洞审计共用同一套校验）；
- 每个许可证表达式必须能解析为合法 SPDX 表达式（空字符串同样视为非法），
  全部非法表达式会在一次 400 中合并报告；
- `allowed` / `denied` 的每一项必须是单个 SPDX 标识符；
- 包名规范化后不得重复，`<root>` 为保留名。

## 重建计划 API

`POST /plan/rebuild` 接收 `/resolve` **成功响应**中的 `resolved` 与
`dependency_graph`，外加本地发生变更的包名列表 `changed`，为需要重建的包生成
确定性计划。规划分三步：

1. **反向传播**：从已安装的 changed 包出发，沿反向依赖边（“谁依赖我”）找出
   全部传递受影响包；
2. **环压缩**：在受影响子图上求强连通分量（SCC），同一环上的包压缩为一个
   必须同批构建的组（自环不会额外产生阶段）；
3. **分层排程**：按“依赖先于依赖者”给组分配阶段——一个组比它依赖的每个组
   晚一个阶段；同一阶段内的组之间没有依赖边，可以并行构建。

```bash
curl -s -X POST localhost:8000/plan/rebuild \
  -H 'Content-Type: application/json' -d '{
  "resolved": {"app": "1.0", "db": "1.0", "web": "2.0", "common": "1.5"},
  "dependency_graph": [
    {"source": "<root>", "source_version": "", "target": "app", "target_version": "1.0"},
    {"source": "app", "source_version": "1.0", "target": "db", "target_version": "1.0"},
    {"source": "app", "source_version": "1.0", "target": "web", "target_version": "2.0"},
    {"source": "db", "source_version": "1.0", "target": "common", "target_version": "1.5"},
    {"source": "web", "source_version": "2.0", "target": "common", "target_version": "1.5"}
  ],
  "changed": ["common", "numpy"]
}'
```

请求字段：

- `resolved`：包名 → 已解析版本（PEP 440），即 `/resolve` 的 `resolved`。
- `dependency_graph`：边列表，即 `/resolve` 的 `dependency_graph`；校验口径与
  漏洞审计完全一致（无悬空边、版本必须与 `resolved` 一致、`<root>` 边的
  `source_version` 必须为空）。
- `changed`：本地变更包名列表，按既有规则规范化（`Foo_Bar` 与 `foo-bar`
  视为同一个包，重复项合并）。出现在 `resolved` 中的作为传播起点；未出现的
  单列为 `uninstalled`，不参与传播。

响应（200）：

```json
{
  "status": "ok",
  "summary": {"changed": 1, "uninstalled": 1, "affected": 4, "groups": 4, "stages": 3},
  "changed": ["common"],
  "uninstalled": ["numpy"],
  "affected": ["app", "common", "db", "web"],
  "groups": [
    {"id": 0, "packages": ["common"], "changed": ["common"]},
    {"id": 1, "packages": ["db"], "changed": []},
    {"id": 2, "packages": ["web"], "changed": []},
    {"id": 3, "packages": ["app"], "changed": []}
  ],
  "stages": [[0], [1, 2], [3]],
  "rebuild_paths": [
    {"package": "common", "distance": 0,
     "path": [{"name": "common", "version": "1.5"}]},
    {"package": "db", "distance": 1,
     "path": [{"name": "db", "version": "1.0"},
              {"name": "common", "version": "1.5"}]},
    {"package": "web", "distance": 1,
     "path": [{"name": "web", "version": "2.0"},
              {"name": "common", "version": "1.5"}]},
    {"package": "app", "distance": 2,
     "path": [{"name": "app", "version": "1.0"},
              {"name": "db", "version": "1.0"},
              {"name": "common", "version": "1.5"}]}
  ]
}
```

- `changed` / `uninstalled`：已安装（参与规划）与未安装（仅报告）的规范化
  changed 包名，均按名称排序。
- `affected`：全部受影响包（含 changed 包本身），按名称排序。
- `groups`：压缩后的构建组；`changed` 字段标出组内的变更包。组按“依赖先于
  依赖者”的确定性顺序编号（Tarjan SCC 发射顺序，根与邻接均排序）。
- `stages`：每个阶段是一组可并行构建的组 id；阶段号严格按依赖关系递增
  （changed 叶子组位于阶段 0；若某个 changed 包还依赖另一个 changed 包，
  它会排在后续阶段，始终满足“依赖先于依赖者”）。
- `rebuild_paths`：每个受影响包距**最近** changed 包的最短反向路径（从该包
  沿依赖方向走到 changed 包，路径节点带已解析版本）；距离相同时按
  （距离, 包名）排序，等距路径按字典序最小的节点序列打破平局。

环依赖中的包会被压进同一个组、在同一个阶段整批重建；例如 `a ↔ b` 且 `a`
变更时，`{"a","b"}` 是阶段 0 的一个组，依赖它们的包进入后续阶段。相同输入
（即使边的给出顺序不同）永远得到相同的分组、阶段与路径顺序。

校验规则（违反即返回 `400`，错误体与 `/resolve` 相同）：

- `resolved` 中的版本必须是合法 PEP 440 版本；
- 边的 `source`（`<root>` 除外）与 `target` 必须出现在 `resolved` 中，否则
  视为悬空边；
- 边的 `source_version` / `target_version` 必须与 `resolved` 中对应版本一致
  （`<root>` 边的 `source_version` 必须为空）；
- `changed` 中的名称必须是非空字符串，规范化后不得为 `<root>`。

## 数据持久化

异步作业保存在 SQLite（WAL 模式），数据库路径由环境变量 `RESOLVER_DB` 控制，
默认 `./data/jobs.db`。`docker compose up --build` 通过命名卷 `resolver-data`
挂载到容器内的 `/app/data`，因此：

- 重建镜像、重启容器后，已排队和已完成的作业都还在；
- 未完成的作业在启动时自动重新入队执行。

如需清空历史，可删除卷：`docker compose down -v`。

## 请求格式

```json
{
  "packages": {
    "<包名>": {
      "<PEP440版本>": {
        "dependencies": { "<依赖包名>": "<版本说明符>" }
      }
    }
  },
  "root": { "<包名>": "<版本说明符>" },
  "locks": { "<包名>": "<锁定版本>" },
  "allow_prereleases": false,
  "marker_env": { "python_version": "3.11" }
}
```

- `packages`：本地包索引，包名 → 版本 → 条目。`dependencies` 可以是
  `{包名: 说明符}` 映射，也可以是 PEP 508 字符串数组，例如
  `["requests>=2.0", "tomli; python_version < '3.11'"]`。
- `root`：根依赖，映射或 PEP 508 字符串数组均可。
- `locks`：可选锁定版本。
- `allow_prereleases`：可选，默认 `false`。
- `marker_env`：可选，覆盖标记求值环境。
- `max_attempts`：可选，候选求值上限（默认 100000）。

### 成功响应（200）

```bash
curl -s -X POST localhost:8000/resolve -H 'Content-Type: application/json' -d '{
  "packages": {
    "app": {"1.0": {"dependencies": {"web": ">=2.0", "db": ""}}},
    "web": {"2.0": {"dependencies": {"common": "<2.0"}},
            "3.0": {"dependencies": {"common": ">=2.0"}}},
    "db":  {"1.0": {"dependencies": {"common": "==1.5"}}},
    "common": {"1.5": {}, "2.0": {}}
  },
  "root": {"app": ""}
}'
```

`web 3.0` 要求 `common>=2.0`，与 `db` 的 `common==1.5` 冲突，于是回溯到
`web 2.0`：

```json
{
  "status": "ok",
  "attempts": 5,
  "resolved": {"app": "1.0", "common": "1.5", "db": "1.0", "web": "2.0"},
  "dependency_graph": [ ... ],
  "locks": {}
}
```

### 锁定优先

```json
{ "packages": {"x": {"1.0": {}, "2.0": {}}},
  "root": {"x": ">=1.0"},
  "locks": {"x": "1.0"} }
```

只要锁仍满足约束，`x` 解析为 `1.0`（而非最高的 `2.0`），`locks.x = "applied"`。

### 冲突响应（409）

```json
{
  "packages": {
    "service-a": {"1.0": {"dependencies": {"libfoo": ">=2.0"}}},
    "service-b": {"1.0": {"dependencies": {"libfoo": "<2.0"}}},
    "libfoo": {"1.0": {}, "2.0": {}, "3.0": {}}
  },
  "root": ["service-a", "service-b"]
}
```

返回（节选）：

```json
{
  "status": "conflict",
  "conflict": {
    "terminal_conflicts": [
      {
        "package": "libfoo",
        "reason": "unsatisfiable",
        "available_versions": ["3.0", "2.0", "1.0"],
        "active_constraints": [
          {
            "requirement": "libfoo>=2.0",
            "required_by": "service-a",
            "required_by_version": "1.0",
            "path": [{"name": "<root>"}, {"name": "service-a", "version": "1.0"}]
          },
          {
            "requirement": "libfoo<2.0",
            "required_by": "service-b",
            "required_by_version": "1.0",
            "path": [{"name": "<root>"}, {"name": "service-b", "version": "1.0"}]
          }
        ]
      }
    ],
    "tree": { "...": "完整的失败分支树（含每步尝试的版本）..." }
  }
}
```

错误请求（非 PEP 440 版本、非法说明符、多余字段等）返回 `400/422` 并附带原因。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pip install pytest httpx
pytest -q
```

本地运行时作业库默认创建在 `./data/jobs.db`（已加入 `.gitignore`），可用
`RESOLVER_DB=/path/to/jobs.db` 覆盖。

## 解析策略说明

- 每个包维护当前所有生效约束的交集；候选必须同时满足全部约束。
- 搜索采用带精确撤销日志的深度优先回溯：选定一个版本后施加其依赖并递归；
  更深层失败时，按逆序撤销该候选引入的全部约束、边与发现标记，再尝试下一候选。
- 求解器仅做“按时间顺序回溯”，保证完备：有解时一定能找到；无解时一定报告。
- 为防止病态输入无限膨胀，`max_attempts` 触顶会返回 `status: "limit"`（仍带
  结构化冲突信息）。

## 项目结构

```
app/
  resolver.py       # PEP 440/508 解析、候选排序、回溯求解器、冲突数据结构、取消点
  serialize.py      # 冲突树 / 终态冲突的 JSON 序列化
  audit.py          # 漏洞审计：公告匹配、图一致性校验、<root> 最短路径枚举
  license_audit.py  # 许可证审计：SPDX 表达式解析、AND/OR/WITH 策略求值、决定性标识符
  rebuild.py        # 重建计划：反向依赖传播、Tarjan 环压缩、依赖优先并行 stages、最短反向路径
  jobs.py           # SQLite 作业存储、BEGIN IMMEDIATE 原子领取、后台 worker
  main.py           # FastAPI 应用与 /resolve、/audit/*、/plan/rebuild、/jobs、/health
tests/              # 端到端 API 测试（含漏洞、许可证审计与重建计划）、作业持久化/取消测试、求解器单元测试（含随机暴力对照）
data/               # 运行时生成：SQLite 数据库（RESOLVER_DB，已 gitignore）
Dockerfile
docker-compose.yml
requirements.txt
```

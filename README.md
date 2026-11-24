# TFTP 丢包重传试跑台（旧交换机镜像取回验证）

网管维修旧交换机前，用来确认 **TFTP 下载即使丢包也能取回完整镜像** 的试跑项目。
Flask 网页可**同时发起两路真实 UDP TFTP 下载**，每路可选择
「**首次首块丢弃一次（不回 ACK，等服务端重传）**」或「正常逐块确认」，
页面实时显示两路的**重传次数**与 **SHA-256 摘要校验**结果。

- 内置一份确定性的 **1024 字节样例镜像**（SHA-256 见网页/`/api/config`）。
- 仅 **RRQ 只读 + `octet` 模式 + 512 字节块**。
- TFTP 服务**最多接 2 路**终端；另带一个服务端 stats JSON 接口。
- 测试终端与 TFTP 服务之间是**真实 UDP 报文**（Compose 中是两个容器跨网络通信；
  本机模式是 loopback 上的真实 UDP socket，不是函数 mock）。

---

## 1. Docker Compose 启动（推荐交付方式）

```bash
cd /workspace
docker compose up --build
```

然后浏览器打开：

- 试跑页： <http://localhost:8080>
- TFTP：宿主机 `udp/69`（标准端口）与 `udp/6969`（高位备用）→ 容器 `69/udp`
- 服务端 stats： <http://localhost:8001/stats>

网页操作：

1. 顶部选择「A 丢首块 / B 正常（推荐对照）」（或两路都丢/都正常）。
2. 点 **⚡ 同时发起两路下载**。
3. 约 1 秒后：
   - A 路显示「重传次数（终端观测）1 次」「重传次数（服务端计数）1 次（超时 1 / 重复RRQ 0） · 终值」，
     SHA-256 **一致 ✓**；
   - B 路重传 0 次（同样标注「· 终值」）、摘要一致、毫秒级完成。
4. 下方服务端面板可看 `slots x/2`、当前待确认块、距原超时点、各类计数。
5. 「🧪 满载拒绝 · 名额归还自检」会真实占满 2 个名额，验证第三路收到
   `ERROR ... server busy ...`，并在最后一次重试超时后归还名额、新终端可正常下载。

> 说明：TFTP 服务采用**单 UDP 套接字**设计，所有 DATA（含重传）都从同一个服务
> 端口发出，因此重复 RRQ 的「从原传输端口补发」以及 Docker UDP 端口映射都最稳妥。

---

## 2. 不用 Docker 的本机运行

需要 Python 3.11+ 与 Flask：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 一键本机试跑（TFTP udp/1069、stats http://127.0.0.1:18001、网页 http://127.0.0.1:18080）
./run_local.sh
```

或者只启动网页（默认会在本机守护线程内自带一个 TFTP 服务，单命令即可）：

```bash
python web.py                       # http://127.0.0.1:8080, tftp 127.0.0.1:1069
```

分别启动两个进程（更接近 Compose 的双服务形态）：

```bash
TFTP_HOST=127.0.0.1 TFTP_PORT=1069 STATS_PORT=18001 \
  TFTP_TIMEOUT=0.8 /path/to/python server.py

TFTP_HOST=127.0.0.1 TFTP_PORT=1069 \
  STATS_URL=http://127.0.0.1:18001/stats WEB_PORT=8080 \
  /path/to/python web.py
```

---

## 3. HTTP / JSON API

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /` | 试跑网页 |
| `GET /api/config` | 镜像元数据、SHA-256、TFTP 目标、模式 |
| `POST /api/both/start` | 同时发起 A/B 两路，body：`{"modes":{"A":"lose-first","B":"normal"}}` |
| `POST /api/lane/<A|B>/start` | 仅启动一路，body：`{"mode":"normal|lose-first"}` |
| `GET /api/state` | 两路状态（终端结果 + 服务端会话：运行中实时匹配，结束后定格本轮终值）+ 服务端 stats |
| `GET /api/server-stats` | TFTP 名额/重传/历史原始 JSON |
| `POST /api/full-test/start` / `GET /api/full-test/state` | 满载拒绝/超时归还自检任务 |

运行中重复开始会返回 `409`，不会冲掉正在进行的传输。

---

## 4. 原始 UDP 协议自检

`scripts/raw_checks.py` 不经过网页，直接用 UDP 报文核对边界语义（26 个断言）：

```bash
# Compose 启动后（宿主机映射到 6969，stats 8001）
python scripts/raw_checks.py 127.0.0.1 6969 http://127.0.0.1:8001/stats

# 或标准 69 端口
python scripts/raw_checks.py 127.0.0.1 69  http://127.0.0.1:8001/stats

# 本机 run_local.sh（1069 / 18001）
python scripts/raw_checks.py 127.0.0.1 1069 http://127.0.0.1:18001/stats
```

覆盖：未知 TID→`ERROR 5`、正常三块（block1/2 + **空 block3**）、丢首块超时重传、
同源端口重复 RRQ（同端口补发 / 不增名额 / **不推迟原超时点**）、
迟到 ACK（忽略 / **不重置超时点**）、满载拒绝第三路、最后重试超时归还、归还后可下载。

另有 `scripts/web_state_checks.py` 针对试跑页统计的回归自检（14 项断言：
丢首块定格、**普通终端复用同一源 IP:端口**不覆盖旧值、**历史滚出 30 条上限**不丢值、
两路各自独立、重跑才更新、两路并发主流程）：

```bash
# 本机双进程（run_local.sh 形态）
python scripts/web_state_checks.py --web http://127.0.0.1:18080 \
  --tftp-host 127.0.0.1 --tftp-port 1069 --stats http://127.0.0.1:18001/stats

# Compose 形态（需在 web 容器网络命名空间内，才能复用相同源 IP:端口）
docker compose exec web python /app/scripts/web_state_checks.py \
  --web http://127.0.0.1:8080 --tftp-host tftp --tftp-port 69 \
  --stats http://tftp:8001/stats
```

---

## 5. 需求 → 实现对照

| 需求 | 实现位置 | 行为 |
| --- | --- | --- |
| 内置 1024B 样例镜像 | `firmware.py: build_image/IMAGE` | 确定性字节，SHA-256 `588be30f…77f4c` |
| 只读 octet / 512B | `firmware.py`、`server.py:_handle_rrq` | 非 octet 回 ERROR；WRQ 回 ERROR(2) |
| 网页同时两路、可丢首块或正常确认 | `web.py` + `terminal.py:download_once` + `static/app.js` | 栅栏让两路同时发 RRQ；`lose-first` 首次 block1 收到后丢弃且不 ACK |
| 查看各路重传次数与摘要 | 网页卡片 + `/api/state` | 同时显示终端观测值与服务端计数，SHA-256 双向核对 |
| 服务端计数随本轮定格 | `web.py:_capture_final_session/_match_run_session` + `server.py` 快照新增 `seq/started_ts/finished_ts` | 每路下载结束即按「源端口+本轮时间窗+会话序号」捕获该轮服务端会话终态并定格；端口复用不改旧值、历史滚出 30 条不丢值、页面重开仍可见，直到该路重新试跑 |
| 服务最多 2 路 | `server.py:MAX_SLOTS` | 按 `(ip,port)` 占名额 |
| 同源 IP:端口重复 RRQ | `server.py:_handle_rrq` | 找到原会话，从原服务端口补发待确认块，**不新增名额** |
| 只有当前块 ACK 推进 | `server.py:_handle_ack` | `ack != sess.block` 一律计为 late/stale 并忽略 |
| 迟到 ACK / 重复 RRQ 沿用原超时点 | `server.py` | 两条路径都**不修改 `deadline`**、重复 RRQ 不消耗超时重试次数 |
| 最后重试超时 / 传完归还名额 | `server.py:handle_timeouts/_close_session` | `MAX_RETRIES`（默认 4）次超时重传后释放；完成即释放 |
| 满载拒绝新终端 | `server.py` + 网页自检 | 回 TFTP `ERROR code=0 "server busy…"`，并累加 `rejected_full` |
| 整数倍 512 需空块 ACK 才算完 | `firmware.LAST_BLOCK_NUMBER=3`、`server.py` | 发完 block2 再发空 block3，收到 `ACK(3)` 才 `completed` |

超时模型：首发时设定 `deadline = now + TFTP_TIMEOUT`；超时后重传并把下一超时点
设为 `now + TFTP_TIMEOUT`（固定间隔，默认 Compose 1.0s / 本机自检 0.8s）。
重复 RRQ 的立即补发与迟到 ACK 都不会移动当前 `deadline`。

### 环境变量

| 变量 | Compose 值 | 直接运行默认 | 含义 |
| --- | --- | --- | --- |
| `TFTP_PORT` | `69` | `1069` | TFTP UDP 端口 |
| `TFTP_TIMEOUT` | `1.0` | `1.0`（`run_local.sh` 用 `0.8`） | ACK 超时秒数 |
| `TFTP_MAX_RETRIES` | `4` | `4` | 超时重传次数上限，之后最后一次超时释放名额 |
| `STATS_PORT` | `8001` | `8001`（`run_local.sh` 用 `18001`） | 服务端 stats HTTP 端口 |
| `STATS_URL`（web） | `http://tftp:8001/stats` | `http://127.0.0.1:18001/stats` | 网页后端拉取 stats 的地址 |
| `TFTP_AUTOSTART`（web） | `0` | `1`（仅 TFTP 指向 loopback 时） | 直接跑 `web.py` 时是否内置 TFTP |
| `WEB_PORT` | `8080` | `8080`（`run_local.sh` 用 `18080`） | Flask 端口 |

---

## 6. 交付说明：做了什么 / 测过什么 / 未完成

### 本次修复（2026-11-24）：两路服务端重传计数随本轮定格

**缺陷**：此前 `/api/state` 在页面每次轮询时才用 `client_port` 去服务端**当前**
sessions/history 里匹配会话。丢首块下载完成后：① 普通终端（或另一路、满载自检
探针）复用同一源 IP:端口产生新会话时，旧结果会被填上新会话的计数（1 变 0）；
② 后续下载把原会话挤出 30 条历史后，计数直接消失（显示「未匹配」）。

**修复内容**：

- `server.py`（纯增量，协议与 30 条历史上限不变）：stats 会话快照新增
  `seq`（进程内单调递增会话序号）、`started_ts`、`finished_ts`（epoch 秒）。
- `web.py`：每路下载一结束，后端 worker 即按「源端口 + 本轮时间窗
  （`started_ts ∈ [本轮开始-2s, 本轮结束+2s]`）+ seq 取最新」定位本轮服务端会话，
  轮询直至其进入 history 拿到**终态**（窗口覆盖末 ACK 丢失时的完整超时重试周期），
  定格进该路 `server_session`。此后 `/api/state` 直接返回定格值
  （`server_session_frozen=true`），不再重新匹配；定格前/运行中仍走带时间窗的
  实时匹配兜底。`_blank_lane` 在该路重新试跑时清空定格值。
- `static/app.js`：定格值标注「· 终值」；`templates/index.html` 判定说明同步补充。
- 新增 `scripts/web_state_checks.py` 回归自检（14 项断言）。

**实测结果**（本交付环境，双进程 Compose 同构形态：server.py + web.py 分离）：

1. `scripts/web_state_checks.py`：**14 passed, 0 failed** —— A 丢首块定格为
   1（超时 1 / 重复RRQ 0）；普通终端绑定**同一源 IP:端口**正常下载（服务端出现
   seq 更大、计数 0 的新会话）后 A 路仍显示旧会话终值 1；再跑 32 次下载把原会话
   挤出 30 条历史后 A 路终值仍在（等价于页面关闭重开场景，捕获在后端完成、
   不依赖浏览器）；B 路独立试跑互不影响；A 路重跑后更新为本轮新值 0；
   两路并发 A=1/B=0 且摘要一致。
2. `scripts/raw_checks.py`：**26 passed, 0 failed**（协议行为无回归）。
3. jsdom 真实加载试跑页、模拟点击：**11 passed, 0 failed** —— 点「同时发起
   两路下载」后 A 路显示「1 次（超时 1 / 重复RRQ 0） · 终值」、B 路「0 次…· 终值」、
   SHA-256 一致 ✓；再点页面内「满载自检」全部通过，且自检（真实占满名额 + 新终端
   下载）后两路终值不变。
4. 运行中实时路径抽查：下载中 `server_session_frozen=false` 且显示活动会话实时
   计数，结束后切换为定格终值。
5. `python -m py_compile` 全部 Python 文件、`node --check app.js`、
   `docker-compose.yml` YAML/服务/端口结构校验均通过。

**余项 / 限制**：

- 本沙箱仍无 Docker daemon，`docker compose up --build` 未实际构建；Compose 仅做
  结构校验，功能实测在同构双进程（同端口/同环境变量/真实 UDP）上完成。
  `docker compose exec web python /app/scripts/web_state_checks.py ...` 为 Compose
  下运行回归脚本的方式（需同网络命名空间才能复用相同源端口）。
- 定格匹配依赖两端时钟偏差 < 2s（`CLOCK_SKEW`）；Compose 下同宿主机时钟一致，
  跨机部署需 NTP 基本同步。
- 末 ACK 丢失的极端情形：定格会等服务端超时重试耗尽（默认约 6s）后取终态，
  期间页面经实时兜底显示推进中的计数；该路径按设计覆盖、未单设故障注入测试。
- 若服务端进程重启，stats 历史清零，已定格的路卡片不受影响（存在 web 后端）。

### 原始交付内容

- `firmware.py`：1024B 确定性样例镜像、SHA-256、TFTP 报文编解码、末块号计算；
  `python firmware.py` 可导出同一份 `sample_image.bin`（已随仓库放置一份，摘要一致）。
- `server.py`：单 UDP socket 的 TFTP 只读服务 + 2 名额调度 + 严格超时点状态机
  + 线程内 stats HTTP（`/stats`、`/healthz`）+ 控制台审计日志。
- `terminal.py`：真实 UDP 测试终端（正常 / 丢首块一次）、重传与摘要统计、
  满载拒绝与名额归还编排探针。
- `web.py`：Flask 页面与 JSON API、两路并发管理、服务端会话按
  「源端口+时间窗+会话序号」关联并在结束后定格、满载自检后台任务、
  可选的进程内 TFTP 自启兜底。
- `templates/index.html`、`static/{style.css,app.js}`：中文试跑台界面，
  800ms 轮询，展示两路指标（含定格「终值」标注）、摘要、事件流水、名额表面板与满载自检。
- `scripts/raw_checks.py`：26 项原始 UDP 协议断言；
  `scripts/web_state_checks.py`：14 项试跑页统计回归断言。
- `Dockerfile`、`docker-compose.yml`、`requirements.txt`、`.dockerignore`、
  `.gitignore`、`run_local.sh`。

### 测过什么（均在本交付环境真实执行）

1. `python -m py_compile` 全部 Python 文件通过；`node --check app.js` 通过；
   `docker-compose.yml` 通过 YAML 解析与服务/端口结构检查。
2. 启动真实 TFTP 进程后，`scripts/raw_checks.py`：
   **26 passed, 0 failed**，包括用微秒级计时验证
   「重复 RRQ / 迟到 ACK 不推迟原超时点」（实测重传仍落在首发后 ~0.80s，
   而不是事件后再等一个 T）。
3. 通过网页同款 JSON API：
   - A 丢首块 / B 正常：A 约 0.8s 完成、双端重传计数均为 1；B 毫秒完成、0 重传；
     两路 1024B、SHA-256 全部一致。
   - 两路都丢首块：服务端 `slots` 一度为 2/2，两路均靠 1 次超时重传恢复。
   - 运行中再次 `POST /api/both/start` 正确返回 `409`。
   - 网页版满载自检通过：2/2 占用 → 第三路收 ERROR → 约 3.6–3.8s 后名额归零
     → 新终端完整下载且摘要一致。
4. 用 **jsdom 真实加载并执行 `static/app.js`、模拟点击页面按钮**：
   两路状态、重传次数、`SHA-256 一致 ✓` 徽标均正确渲染，页面内满载自检通过。
5. 额外验证了「单命令自启」模式（独立端口上 `python web.py` 内置 TFTP）端到端通过。

### 还有哪些未完成 / 限制

- **本沙箱没有 Docker daemon / `docker` CLI**，因此 `docker compose up --build`
  未能在本机实际构建运行；Compose/YAML 已做结构校验，Dockerfile 仅 `pip install
  -r requirements.txt`（Flask==3.1.3，已在等价 venv 中安装并运行验证）。
  首次在有 Docker 的机器上构建仍需实际跑一次确认。
- Flask 使用内置开发服务器（试跑/单机验证足够），未接 gunicorn/uwsgi 等生产 WSGI。
- 按任务范围只实现 RRQ/octet/512；**未实现** TFTP 选项协商（RFC2347/2348 `blksize`
  等，收到选项会忽略并仍用默认 512）、netascii/mail、WRQ、块号回绕处理
  （镜像仅 1024B，远不到 65535 块）。
- 服务端为演示采用**单 UDP 端口/TID**模型而非 RFC 建议的每会话临时 TID；
  对本试跑页与宿主机 UDP 端口映射更稳，且同样满足“原传输端口补发”。
  若评分用标准 TFTP 客户端并强制要求独立 TID 端口，需要再改为每会话 socket
  （Docker 下还需配置 UDP 端口范围/host 网络或 conntrack TFTP helper）。
- 未做鉴权与多用户隔离；`/api/*` 仅适合本地/可信网络试跑。
- 没有提交 git commit（按约定仅在用户明确要求时提交）；所有改动都在工作区未跟踪文件中。

# 星载递归载荷兼容审计

在星载指令发送端升级载荷定义前，对“发送端（生产者）可能产生的每一种递归载荷”与
“在轨接收端（消费者）接受的契约”做**协归结构子类型**裁决，并冻结审计结论。

## 判定语义

方向固定：发送端 ≤ 接收端（发送端是生产者，接收端是消费者）。

- **记录**：接收端要求的必需字段，发送端必须提供；字段值逐字段兼容。
  - 发送端携带接收端未要求的额外字段合法（记录宽度子类型）。
  - 接收端可选字段缺失合法；若发送端提供了该字段，其值仍须兼容。
- **变体**：发送端可能发出的每个标签都必须在接收端标签集合内，载荷逐标签兼容。
  - 接收端声明更多标签合法（发送端永不产生即可）。
- **基本类型** `int` / `bool` / `text` 按名不变，`bool` 不视为 `int`。
- **具名引用 `ref`**：以**协归关系**裁决。比较对 `(发送类型, 接收类型)` 在当前
  证明分支上再次出现时，作为余归纳假设直接成立——不展开到固定深度，也不用抽样实例
  代替。递归闭合点会在结果 `reused_pairs` 中按类型路径标出。
- 首个违约按**稳定的类型路径**选择：记录字段按接收端声明顺序（缺失先于更深层违约在同
  一字段上的判定），变体先按接收端标签顺序裁决共有标签载荷，再按发送端声明顺序报告超出
  接收端的标签。

每套声明至多 24 个具名类型，列表中的**第一个类型为该侧根类型**。

### 静态问题一次性反馈

未定义引用、重复类型名、记录内重复字段、变体内重复标签、非法标识（类型名/字段名/标签
名/引用名）、无保护别名环（如 `A = B, B = A` 这类环路上没有 record/variant 保护点）、
每侧超 24 个类型等，都会在一次响应中全部列出；存在静态问题时结论为 `invalid`，不进入
兼容裁决。

### 结论冻结

- 相同 `audit_id` + 相同契约重传：返回**原冻结结论**（HTTP 200，`reused_frozen=true`）。
- 相同 `audit_id` + 任一契约变化（即使新契约非法）：**HTTP 409 拒绝，绝不清写**。
- 非法契约不占用审计标识，修正后可用同一标识重新提交。
- 冻结记录持久化在 `/data/audits.json`（原子写），服务重启后仍可重开。

契约变化按规范化 JSON（去空白、键序排序）后的 SHA-256 判定。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康状态 |
| `POST` | `/api/audits` | 提交/重传审计（201 新建冻结，200 复用冻结，409 契约冲突，400 审计标识非法） |
| `GET` | `/api/audits/{audit_id}` | 重开冻结结论并回填原契约 |
| `GET` | `/` | 审计页面（通过真实 fetch 调用上述 API） |

提交体示例：

```json
{
  "audit_id": "mission-X.cmd.payload.v3",
  "sender": [
    { "name": "Cmd", "type": { "kind": "record", "fields": [
      { "name": "id", "type": { "kind": "int" }, "required": true },
      { "name": "body", "type": { "kind": "ref", "name": "Payload" }, "required": true }
    ]}},
    { "name": "Payload", "type": { "kind": "variant", "tags": [
      { "label": "Arm", "type": { "kind": "record", "fields": [
        { "name": "delay", "type": { "kind": "int" }, "required": true },
        { "name": "next", "type": { "kind": "ref", "name": "Payload" }, "required": false }
      ]}},
      { "label": "Ping", "type": { "kind": "bool" } }
    ]}}
  ],
  "receiver": [ "...同构声明，允许更宽..." ]
}
```

类型节点 `kind`：`int` | `bool` | `text` | `record` | `variant` | `ref`。

## 容器化运行

```bash
# 宿主机端口可用 AUDIT_PORT 覆盖（默认 8080）
AUDIT_PORT=8080 docker compose up --build -d web
curl -s http://127.0.0.1:8080/healthz
# 浏览器打开 http://127.0.0.1:8080/
```

单次验收组件（引擎复核 → pytest → 构建检查 → 对运行中的 web 做真实 HTTP 冒烟），
退出码 0 通过、非 0 失败：

```bash
docker compose build web verify    # 如需要
docker compose run --rm verify
# 或一条：构建并等待 web 健康后执行 verify（verify 退出后停止）
AUDIT_PORT=8080 docker compose up --build --abort-on-container-exit verify
```

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
AUDIT_BASE_URL=http://127.0.0.1:8000 .venv/bin/python verify.py   # 真实 HTTP 验收
.venv/bin/python verify.py                                        # 不带地址时走完整 ASGI 请求链路
```

## 目录

- `app/core.py` — 编译、静态校验、协归结构子类型裁决（纯函数核心）
- `app/storage.py` — 冻结结论的原子持久化与契约哈希
- `app/main.py` — FastAPI 路由
- `app/static/index.html` — 提交/重开页面
- `tests/test_audit.py` — 29 项引擎与 API 测试
- `verify.py` — compose 单次验收组件

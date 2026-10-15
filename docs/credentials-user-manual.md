# Argus 凭据使用手册

> 写给第一次接触 Argus 的人。不需要懂代码，照着做就能完成配置和日常管理。
> 进阶设计说明见 [docs/secret-providers.md](secret-providers.md)，本文只讲"怎么做"。

---

## 一、先花两分钟搞懂背景

**Argus 是干什么的？**
它是一个自动给 AI Agent 做质量测试的平台。测试的方式很直接：Argus 代替用户去调用你们部署的 Agent 服务，收集回答，再打分。

**"凭据"是什么？**
你们的 Agent 服务通常不让人随便调用，调用时要带一个"通行证"（一个叫 Token 的字符串，相当于账号密码）。Argus 要测试它，就得保存这个通行证——保存下来的这份东西，就叫**凭据**。

**把 Token 交给 Argus 安全吗？这套系统的核心承诺是：**

- Token 存进数据库之前会被**加密**（用一把专门的钥匙，钥匙不在数据库里）；
- Token **只在保存时输入一次，之后任何人都看不到原文**，包括你自己；
- Argus 调用你们的 Agent 时自动把 Token 带上，全程不写进日志、不进截图、不回显到报错里。

**你要做的事，按顺序就三件：**

```
① 部署 Argus 的人：生成两把"钥匙"，启动服务（只做一次）
② 管凭据的人：在网页上创建凭据，把 Agent 的 Token 存进来
③ 管凭据的人：把凭据绑定到对应的 Agent（批准"Token 可以发到这个地址"）
```

之后的日常就是：Token 旧了就**换**，出事了就**停**，确认没人用了才**删**。

---

## 二、第一次配置（部署 Argus 的人看，只需做一次）

先说清楚这两个文件是谁用的：它们是 **Argus 自己**的配置，放在**部署 Argus 的那台服务器**上——也就是你执行下面 `docker compose` 命令、跑 Argus 服务的那台机器。**被测试的 Agent 那边什么都不用装、什么都不用放**：Agent 只是在被调用时收到一个 Token 请求头，跟这套文件无关。如果数据库、Langfuse 拆到了别的机器，这两个文件仍然放在跑 Argus 服务（eval-runner）的那台宿主机上。

你要准备**两个文件**，就像配一把保险箱钥匙和一个管理员密码：

| 文件 | 大白话 | 作用 |
|---|---|---|
| **keyring.json**（主密钥） | 保险箱钥匙 | 加密、解锁所有凭据的 Token |
| **admin-token**（管理授权） | 管理员密码 | 以后在网页上添加 / 修改 / 删除凭据，都要出示它 |

### 步骤 1：生成这两个文件

在**部署 Argus 的这台服务器**上找一个安全的目录（示例用家目录，正式部署请放到你们的密钥管理位置），执行：

```bash
umask 077
mkdir -p "$HOME/.config/argus-secrets"
python - <<'PY'
import base64, json, os, secrets
from pathlib import Path
root = Path.home() / '.config/argus-secrets'
(root / 'keyring.json').write_text(json.dumps({
    'active_key_id': 'key-1', 'keys': {'key-1': base64.b64encode(os.urandom(32)).decode()}
}))
(root / 'admin-token').write_text(secrets.token_urlsafe(32))
PY
chmod 400 "$HOME/.config/argus-secrets/"*
```

这会生成两个文件，并把权限锁成"只有文件主人能读"。**这个权限不能放宽**——如果别的账号也能读，Argus 会拒绝工作（这是在保护你，不是找麻烦）。

再记两条规矩：

1. **keyring.json 千万不要和数据库备份放在一起。** 数据库被偷走是打不开的（里面只有密文），但小偷如果连钥匙一起拿走就完了。所以钥匙要单独备份、单独保管；
2. 这两个文件一旦丢失：**admin-token 丢了可以重新生成一个**（旧的作废）；**keyring.json 丢了就只能从你自己的备份恢复**，否则存过的 Token 全部作废、只能重录。

### 步骤 2：启动服务

```bash
export ARGUS_MANAGED_SECRET_KEY_SOURCE="$HOME/.config/argus-secrets/keyring.json"
export ARGUS_CREDENTIAL_ADMIN_TOKEN_SOURCE="$HOME/.config/argus-secrets/admin-token"
docker compose --env-file .env.poc -f docker-compose.yml -f deploy/credentials.compose.yml up -d
```

前两行的意思只是告诉服务"钥匙和管理员密码放在哪"。服务启动后会把这两个文件挂载进容器使用。

**启动之后，部署的人还剩最后一件事**：把 admin-token 文件里的内容（一串长字符）**安全地交给负责管凭据的同事**——走密码管理器或私聊，别贴在群公告、工单、邮件正文里。这串字符就是以后所有凭据操作的"管理员密码"。

到这里，部署收工。剩下都是网页操作。

---

## 三、日常管理（网页 Console 操作）

打开 Console，进入 **Credentials**（凭据）页面。所有凭据操作都在这一页，共四种：

| 我想…… | 点什么 | 需要准备 |
|---|---|---|
| 存入一个新 Token | **创建凭据** | Agent 的 Token + 管理员密码 |
| 换一个新 Token | **轮换** | 新的 Agent Token + 管理员密码 |
| 紧急叫停 | **停用** | 凭据名称 + 管理员密码 |
| 清掉不要的 | **删除** | 凭据名称 + 管理员密码 |

每次操作的弹窗里都有"管理授权 Token"一栏——**把管理员密码填进去**，操作只对这一次生效，浏览器不会记住它。

### 3.1 创建凭据（把 Agent 的 Token 存进来）

点 **创建凭据**，逐项填写：

- **名称**：起个一眼认得出的名字，比如 `客服Agent-生产`。以后到处都用这个名字认凭据；
- **环境**：一个分组标签，最常用 `production`（生产）和 `staging`（预发）。**先记住一条规则：凭据的环境必须和它服务的 Agent 环境一致**，系统不允许混用（防止测试 Token 被发到生产）；
- **Agent Token**：你们 Agent 的通行证原文。**只让填一次，保存后再也看不到**，所以先在密码管理器里备好再粘贴。注意别带换行或空格——从别处复制时最容易带进来，带了会保存失败；
- **管理授权 Token**：管理员密码。

填完点确认。成功后页面上只剩名称、环境、版本号这些信息——Token 原文已经加密收起来了，谁也查不回来。这是正常现象。

### 3.2 绑定到 Agent（一次性的接入手尾）

光存进凭据还不够，还得告诉 Argus："调用某个 Agent 的时候，用这个凭据"。

在哪绑？去 **Agents** 页面创建 **Agent 版本**（就是登记"要测的 Agent 住在哪个地址、怎么调用"）时，有一个凭据下拉框，选你刚创建的凭据即可。**不想设防的 Agent 可以不选**，那就不用管凭据这整件事。

这里有个别嫌麻烦的环节：**选了凭据，就必须再填一次管理员密码**。原因很实在——绑定等于批准"Argus 以后会把你的 Token 发到这个地址"。如果没有这道批准，任何人都可以把你的 Token 引到他自己搭的假 Agent 上偷走。所以每次绑定都当作一次"端点可信"的签字。

绑好之后不用再管：Argus 每次调用这个 Agent，都会自动把 Token 带上。

### 3.3 轮换（换 Token）

**什么时候换**：定期换、怀疑泄漏、或者对方（Agent 那边）要求换。

**怎么做**：凭据列表里点 **轮换**，贴入新的 Agent Token，填管理员密码，确认。

**效果**：凭据本身不变（名字、绑定关系都不动），只是记住的 Token 换成了新的。**不需要重新绑定、不需要改任何配置**。已经在跑的测试任务里，正在进行的那一单还是用旧 Token（它开始前就已经取好了），下一单自动用新 Token。

### 3.4 停用（紧急拉闸）

**什么时候用**：发现 Token 可能泄漏，先拉闸再说。

**怎么做**：凭据列表里点 **停用**。有两个确认：一是**输入凭据的名称**（防手滑）；二是如果这个凭据还在被某个 Agent 用着，要**再勾一个"强制停用"**（也是防手滑）。

**效果**：从这一刻起，所有依赖它的调用会**在发出请求之前直接失败**并报 `CREDENTIAL_UNAVAILABLE`——也就是"宁可不测，也不把可疑的 Token 发出去"。注意：**已经发出去的请求追不回来**，所以拉闸要趁早。

之后想恢复？停用是单行道，不能取消。等换好新 Token 后，重新创建一份凭据再绑定到新的 Agent 版本即可。

### 3.5 删除

**规则一句话：只有"从没被任何 Agent 版本用过"的凭据才能删**，绑定过的只能停用。这是为了保证历史测试记录永远能追溯到"当时用的是哪份凭据"。

**怎么做**：点 **删除**，输入凭据名称确认。如果报 409，说明还有 Agent 版本引用着它——回去走停用流程。

---

## 四、必须知道的几条规则（考试重点）

1. **环境要对上**：凭据的环境（如 `production`）必须和 Agent 版本的环境一致，否则绑定直接被拒。创建 Agent 版本时没填环境的，一律按 `production` 算；
2. **Token 保存后不可查看**：忘了就只能走"轮换"换一个新的，没有找回功能；
3. **停用挡不住已发出的请求**：它是给未来拉闸的，不是撤回键；
4. **换 Token 不用改配置**：绑定关系永远指向凭据本身，Token 是凭据肚子里的可换内容。

---

## 五、出问题了怎么办

| 你看到的 | 意思 | 怎么办 |
|---|---|---|
| 写操作返回 503 "authorization is not configured" | 服务没配管理员密码 | 找部署的人按"步骤 1"准备 admin-token 并重启 |
| 403 "authorization denied" | 管理员密码填错了 | 重新向部署管理员索取，注意别复制到空格换行 |
| 创建 / 轮换报 422 "Invalid credential secret" | Agent Token 不合法 | 检查是否带了换行、空格或非英文字符，重新复制 |
| 绑定报 "Credential is missing, disabled or outside AgentVersion environment" | 凭据不存在、已停用或环境对不上 | 核对凭据状态和环境是否一致（见规则 1） |
| 测试跑一半报 `CREDENTIAL_UNAVAILABLE` | 凭据被停用了，或服务端钥匙文件有问题 | 先看凭据是不是被停用；再找部署的人检查 keyring 文件权限 |
| 删除报 409 | 凭据还有人引用 | 不能删，改用停用 |
| `env://` 开发引用在生产被拒 | 预期行为 | 生产请改用托管凭据（见下节"本地开发"） |

---

## 六、进阶话题（需要时再看）

### 本地开发可以临时"不走保险箱"

开发环境图省事，可以直接用环境变量当 Token，不用创建加密凭据。在 `.env.poc` 里：

```text
ARGUS_SECRET_MODE=development
ARGUS_ALLOWED_CREDENTIAL_ENVS=DEMO_AUTH_TOKEN
DEMO_AUTH_TOKEN=<本地演示 Token>
```

然后在 Agent 版本的"遗留引用"里填 `env://DEMO_AUTH_TOKEN`。**生产环境这套全部禁用**（包括历史上留下的 `env://` 引用，跑起来会直接报错）——上线前请迁移到正式凭据：创建凭据 → 创建新的 Agent 版本并选中它 → 用新版本发测试。

### 定期更换"保险箱钥匙"（主密钥轮换）

平时不用碰。安全规范要求定期换钥匙时，流程是：暂停写操作 → 在 keyring.json 里加一把新钥匙并设为默认（**旧钥匙先留着**）→ 调用 `POST /api/v1/credentials/rewrap`（需管理员密码），系统会把所有存过的密文换到新钥匙上，任何一个解不开就整体撤销重来 → 全部确认换完后，才能删旧钥匙。**旧钥匙没删干净之前别删，旧备份恢复也还需要旧钥匙。**

### 安全边界（一句话版）

这套机制防的是**"数据库或备份被偷"**——偷走也解不开；不防**"服务器本身被攻破"**——服务器失守钥匙也会一起丢。所以正式上线时，Console 和 API 前面还要有你们自己的网关登录（SSO）和 HTTPS，这是部署方的责任。

---

## 附：API 一览（给想用脚本操作的人）

| 方法与路径 | 管理员密码 | 说明 |
|---|---|---|
| `GET /api/v1/credentials` | 不需要 | 列表（只返回元数据，永远没有 Token） |
| `GET /api/v1/credentials/{id}/usage` | 不需要 | 查谁在用它 |
| `POST /api/v1/credentials` | 需要 | 创建 |
| `POST /api/v1/credentials/{id}/rotate` | 需要 | 轮换 |
| `POST /api/v1/credentials/{id}/disable` | 需要 | 停用（要传 `confirm_name`；有引用时加 `force: true`） |
| `POST /api/v1/credentials/rewrap` | 需要 | 换保险箱钥匙时重包裹密文 |
| `DELETE /api/v1/credentials/{id}?confirm_name=...` | 需要 | 删除 |
| `POST /api/v1/agent-versions`（带 `credential_id`） | 需要 | 绑定凭据（= 批准端点） |

管理员密码通过请求头提交：`Authorization: Bearer <admin-token 内容>`。

# 微信外部群关键词记账机器人 V1.0

依据：`微信外部群关键词记账机器人_V1.0_开发文档`（薪火电商）

## 架构

```
企微外部群 → Windows 微信网关(pyweixin) → Django API → 网页控制台 / 薪火推送
```

- **专用普通微信号**加入外部群  
- Windows 只跑精简网关（Worker + Django），浏览器管理  
- 数据库事务成功后才回复成功  

## 部署到新电脑（云电脑）

**本机：** 双击 `打包上传.bat` → 生成 `桌面\xinhuo-bot-deploy.zip`（约 0.3MB，不含 `.venv`、日志、缓存）。
带现有数据库；不想带就用 `打包上传.bat nodb`。

**云电脑：**

1. 登录微信；上传 zip 并解压
2. 双击 `一键部署.bat`（自动装 Python 3.12、建 `.venv`、装依赖、生成 `config\.env`、初始化数据库、自检，可重复运行）
3. 打开 `config\.env`，填 `XINHUO_BOT_PUSH_API_KEY`（不填则记账成功但不推送到薪火）
4. 双击 `启动网关服务.bat`，再双击 `打开网页控制台.bat`
5. 登录：`admin` + 打包数据库里的原密码（空库才是 `admin123`）。忘了就在项目目录执行 `.venv\Scripts\python.exe manage.py changepassword admin`
6. 把要记账的群单独打开并置顶，约 30 秒内出现在概览页

> 一键部署会为云端生成**新的**随机 `DJANGO_SECRET_KEY` 和 `GATEWAY_HMAC_SECRET`，不会沿用本机的。

### 听群的已知限制：「我的群昵称」

Worker 读不到你自己在群里的昵称（当前微信版本不暴露该控件），你自己发的指令会被判为「未知发送人」而被拒（E004）。
临时办法：在 `data\room_my_alias.json` 里按 `"群名": "你的群昵称"` 补一条，例如：

```json
{ "记账-清账": "啊吧(跨境版)" }
```

只读测试：`.env` 里加 `WORKER_NO_SEND=1`，Worker 只收消息入库，不向微信发任何回复。

### 长时间运行的 CPU 占用

群窗口一直开着时，微信会把收到的消息都留在窗口里。旧版每轮都遍历整窗、解析全部历史气泡，开销随时间增长，约一周会把 CPU 压满。现在的做法：

- 每轮只解析末尾 `WECHAT_TAIL_ITEMS` 条（默认 30），并缓存消息列表控件，不再每轮遍历整窗
- 每天 `WECHAT_WINDOW_RESET_AT`（默认 04:00）自动关闭并重开监听的群窗口，恢复原位置和置顶；微信不用重新登录。重开会短暂操作微信主窗口
- 系统 CPU 持续 ≥90% 时 Worker 自动放慢轮询，回落后恢复
- 日志每 10 分钟输出一行 `perf ...`：每轮耗时、每群耗时、每轮解析条数。耗时如果仍随天数增长，说明还有别处在累积

参数见 `.env.example` 的「性能 / 防卡死」一节。

局域网访问：把 `启动网关服务.bat` 里的 `127.0.0.1:8000` 改成 `0.0.0.0:8000`，并在 `config/.env` 写 `DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,你的电脑IP`。

## 指令示例

```
飞机号 金额50 给我50
gpt代充 金额200 给你120 给我50 利润50
查询
清账
查询本月
记账
```

## 目录

- `apps/panel/` — 网页控制台（概览 = 状态 + 听群 + 薪火对接；另有员工记单 / 记账 / USDT / 日志）  
- `gateway/` — 微信 Worker  
- `apps/ledger/` `apps/rules/` `apps/chatrooms/` — 业务  
- `一键部署.bat` / `打包上传.bat` / `启动网关服务.bat` / `打开网页控制台.bat` / `停止网关服务.bat`  

## 测试

```bat
pip install -r requirements-dev.txt
pytest tests/unit -q
```

# 全球健康创新试点运营服务

这是一个面向健康科技展会、临床合作机构、康复机构和产业伙伴的 Python 后端。服务使用 FastAPI 与 SQLite 管理健康创新产品、试点场地、证据材料、公众体验反馈、参数化体验方案、排队场次、站点租约、失败恢复、观察记录版本和人工干预。所有运行状态保存在单个本地数据库文件中，不需要另行部署数据库、缓存、消息队列或浏览器界面。

## 已有能力

- 产品目录：登记来源国家、所属机构、产品类别、用途、风险级别与当前合规状态。
- 场地目录：维护展会体验点、医院、康复机构、研究机构和产业伙伴的能力与并发上限。
- 证据材料：按产品保存临床、性能、安全、合规和体验材料，使用内容摘要实现重复提交幂等，并支持接受或驳回。
- 体验反馈：按场地、场次引用和受众类型保存评分、标签、意见以及后续联系授权，重复反馈不会创建第二条记录。
- 体验方案：使用参数规则描述外骨骼、辅助诊断、数字疗法、慢病管理和数字中医等设备或服务的运行边界。
- 场次调度：提交方按项目和幂等键创建场次，执行站点按能力领取并获得有期限的租约。
- 执行回执：站点可以续租、提交观察记录或报告失败；可重试失败按照确定的退避时间重新排队。
- 失败恢复：租约到期后由恢复入口重新排队，达到最大尝试次数的场次转为失败。
- 人工干预：取消、人工重试、优先级调整和批量操作均保留操作者、原因、前后状态与批次标识。
- 身份审计：保留用户、角色、团队、会话、权限、审计事件和后台维护能力，敏感凭据只保存摘要。
- 匹配推荐：结合产品类别、风险级别、适用人群、地区、场地与科室能力、伦理准备情况和开放时间，对产品×场地给出 0–100 分、带逐条理由的候选顺序；硬性阻断（如高风险产品落到无对应能力或伦理未就绪的科室）明确列为不合格并说明原因。
- 容量预留：运营员依据推荐发起限时预留，医院可以接受、拒绝或附加条件，企业确认后名额才正式占用；并发的双方确认通过即时事务和条件更新保证只有一个成功，不会产生第二个名额。
- 时限与联动：医院答复时限和企业确认时限均可控（环境变量或按次指定），到期由时间规则自动释放；产品暂停或场地能力变化只撤销尚未生效（pending/conditional/accepted）的匹配，已确认名额不受影响，撤销原因通过站内消息送回医院与企业。
- 容量台账：推荐、预留、医院答复、企业确认、释放或生效每一步都追加不可变台账，可按时间窗和场地逐步核对持有/确认余额；值班仪表盘列出等待答复与即将到期的记录，所有状态落盘，服务重启后依然可见。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/health-innovation.db`。可复制 `.env.example`，并通过 `HEALTH_INNOVATION_DATABASE_PATH` 指定其他本地文件。

## 数据库初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
python tools/verify_sqlite.py
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

产品、场地、证据和体验反馈接口使用 `/api/catalog` 前缀，体验方案、场次、领取、回执与恢复接口使用 `/api/pilots` 前缀，匹配推荐、容量预留、到期扫描和容量台账使用 `/api/matching` 前缀。

## 测试

```bash
python -m pytest
```

测试覆盖身份与审计、产品和场地登记、证据重复提交、证据审阅、反馈幂等、参数校验、场次提交、优先级领取、能力匹配、租约续期、失败退避、观察版本、取消、人工重试、批量操作、租约恢复，以及候选排序理由、容量预留双方确认、并发确认唯一成功、到期释放、产品暂停与场地能力变化联动、重启持久化和容量台账。

## 编译检查

```bash
python -m compileall -q app tests tools
```

## API 与命令行冒烟

```bash
python -m app.cli smoke
python -m app.cli pilot-demo
python -m app.cli matching-demo
```

`smoke` 在进程内检查根路径和健康接口。`pilot-demo` 会登记一个康复设备和体验场地，创建外骨骼步态体验方案，提交并领取场次，用于快速确认目录与试点运营链路。`matching-demo` 会登记高风险数字疗法与医院、伦理画像和承接科室，给出带理由的推荐顺位，再走限时预留、医院附加条件、企业确认和容量台账全链路。

## 目录结构

```text
app/
  catalog/          健康产品、试点场地、证据材料与公众反馈
  pilots/           体验方案、场次、租约、观察记录和人工干预
  matching/         候选评分、容量预留、双方确认、到期释放与容量台账
  api/              用户、角色、团队、认证、审计和系统接口
  core/             时钟、安全、隐私、异常与分页
  repositories/     身份、审计和团队数据访问
  services/         身份、后台任务、维护和通用服务
  cli.py            初始化、检查和冒烟入口
  database.py       SQLite 连接、事务、表结构与基础权限
tests/               核心、目录、试点运营和身份回归测试
tools/               数据库完整性检查
```

## 数据一致性

SQLite 连接启用外键、WAL、busy timeout 和同步写入策略。产品目录、证据审阅、反馈提交、场次领取、执行回执与人工干预使用即时事务；领取通过条件更新避免同一场次被重复分配。容量预留同样使用 `BEGIN IMMEDIATE` 串行化写事务，医院答复与企业确认都以条件更新推进状态，并发确认最多一个成功；容量变化只追加台账、不覆盖历史。服务保存 UTC 时间字符串，测试可注入固定时钟验证退避、租约到期、预留时限和跨日配额。审计记录会清理密码、令牌等敏感字段，体验反馈仅保存联系人摘要和是否允许后续联系。

## 匹配与容量预留接口

```text
PUT   /api/matching/products/{code}/eligibility      维护产品适用人群、必备能力、限定地区、伦理状态与开放窗口
POST  /api/matching/sites/{code}/departments         登记院内科室（能力、可承接最高风险、伦理是否就绪）
PATCH /api/matching/sites/{code}/departments/{id}    调整科室能力与承接条件
POST  /api/matching/recommendations                  计算带理由和阻断说明的候选顺位
POST  /api/matching/reservations                     运营员依据推荐发起限时预留（支持幂等键）
POST  /api/matching/reservations/{code}/hospital-response   医院 accepted / rejected / conditional
POST  /api/matching/reservations/{code}/enterprise-confirm  企业确认后正式占用容量
POST  /api/matching/reservations/{code}/cancel       运营方取消未生效预留
POST  /api/matching/sweep-expired                    按可控时间规则释放到期预留（启动时自动执行一次）
GET   /api/matching/dashboard                        等待答复、即将到期与待释放记录
GET   /api/matching/capacity-timeline                时间窗内容量从推荐、预留到释放/生效的逐步台账
GET   /api/matching/messages                         医院/企业/运营员接收撤销与答复原因通知
POST  /api/matching/products/{code}/suspend|resume   产品暂停联动撤销未生效匹配
PATCH /api/matching/sites/{code}/capabilities        场地能力变化联动撤销不再合格的未生效匹配
```

# 审计取证授权与程序留痕

面向修订后审计程序的统一取证与程序控制服务：把财政收支、国有资源和重大公共
工程纳入同一案件脉络，同时以角色权限、逐案授权和事件水位保证普通项目权限
看不到金融账户查询材料、也无法干预受限登记。

## 能力总览

| 程序要求 | 实现位置 | 关键控制 |
| --- | --- | --- |
| 年度计划、审计通知、事项范围、审计组成员只按批准版本生效 | `services/documents.py` | 草稿不生效；`effective()` 只返回 approved 版本；驳回不影响旧版本 |
| 临时扩大范围须说明法定依据，并由未参与提出的人复核 | `services/documents.py` | 无法定依据拒绝起草；提出参与人进入回避名单；复核通过且批准人分离后才生效 |
| 电子资料保存摘要、来源、承诺、到达时间 | `services/evidence.py` | 四要素随首交固定 |
| 同交付号重试沿用原事实 | `services/evidence.py` | 摘要一致即重放原事实，仅追加重试轨迹 |
| 异文转入证据争议 | `services/evidence.py` | 原事实不变，异文落争议队列，绝不静默覆盖 |
| 账户查询签发三核验 | `services/account_queries.py` | 签发权限 + 两名不同执行人员（均具执行权）+ 通知书期限（已批准生效、未届满、范围内含） |
| 交金融机构信息限于协助所需 | `services/account_queries.py` | 字段白名单 + 账户/期间/事项不得超出申请范围 |
| 回执不能反向改写申请 | `services/account_queries.py` | 申请快照摘要锚定；回执只追加，篡改即 `ReceiptConflict` |
| 打探干预/拒绝配合/跨机关协助分离 | `services/restricted.py` | 三类独立记录、独立权限、独立队列；干预队列仅机关负责人可阅 |
| 更正只增版本不删原记录 | `services/restricted.py` | 新版本携带更正理由，历史版本永久保留 |
| 取证、封存、范围变更同水位原子提交 | `service.submit_case_batch` | 单事务乐观水位；任一失败整批回滚，不会“材料已受限而决定未生效” |
| 授权查询可解释 | `account_queries.explain()` | 证据如何取得、何人曾在何权限下使用、程序当前是否有效 |
| 停机恢复后期限继续执行 | `services/deadlines.py` + `recover()` | 意见期限、协助催办（阶梯循环）、复核工作全部持久化，重启即续 |

## 权限隔离

- 角色权限定义在 `constants.py` 的 `ROLE_PERMISSIONS`；
- 金融账户查询材料的查看（`account_material.read`）**不在任何普通项目角色的
  默认权限中**，仅在签发时逐案授予登记的两名执行人；
- 金融材料登记/封存（`account_material.record`）仅机关负责人序列拥有；
- 案件时间线（`cases.timeline`）按查看者的有效权限过滤事件，无权事件连类型
  都不暴露。

## 模块布局

```
src/audit_evidence_procedure/
├── service.py            # 门面：组装服务、联合批处理、停机恢复
├── constants.py          # 案件类别、文书种类、角色、权限矩阵、期限类型
├── clock.py              # 可注入时钟（生产 UTC / 测试可拨快）
├── errors.py             # 业务错误体系
├── storage.py            # SQLite：全部业务表 + 事件日志 + 案件水位
├── context.py            # 领域资料读取
└── services/
    ├── access.py         # 人员、角色权限、逐案授权
    ├── cases.py          # 统一案件脉络、水位批处理、权限过滤时间线
    ├── documents.py      # 批准版本文书与扩范围复核
    ├── evidence.py       # 电子交付、证据争议、封存
    ├── account_queries.py# 账户查询授权链与授权解释
    ├── restricted.py     # 三类受限记录与版本更正
    └── deadlines.py      # 程序期限队列（持久化、催办阶梯）
```

## 使用示例

```python
from audit_evidence_procedure import AuditEvidenceProcedureService, MutableClock

svc = AuditEvidenceProcedureService("audit.db", clock=MutableClock())
svc.register_person("d", "负责人", "director")
svc.register_person("a1", "审计甲", "auditor")
svc.register_person("a2", "审计乙", "auditor")
svc.register_person("r", "复核人", "reviewer")
svc.register_person("u", "被审计单位", "audited_unit")

# 统一案件：财政收支 / 国有资源 / 重大公共工程同一脉络
svc.open_case("C1", "fiscal", "预算执行审计", "d")

# 批准生效的审计通知书（草稿不生效）
v = svc.documents.draft("C1", "audit_notice", {...}, "d")
svc.documents.approve("C1", v, "d")

# 账户查询：签发三核验通过后，仅两名执行人获得逐案材料查看权
no = svc.account_queries.create_request("C1", "某单位", ["ACC-1"],
                                        "2026-01-01", "2026-03-31", ["交易流水"], "a1")
svc.account_queries.sign("C1", no, "d", ["a1", "a2"], valid_from, valid_until)
svc.account_queries.prepare_assistance_package("C1", no, {...最小必要字段...}, "a1")

# 取证、封存、范围变更同水位原子提交
svc.submit_case_batch("C1", [
    {"op": "register_delivery", "delivery_no": "D-1", ...},
    {"op": "seal_delivery", "delivery_no": "D-1", "sealed_by": "d"},
    {"op": "approve_scope", "version_id": reviewed_scope_id, "approver_id": "d"},
], expected_watermark=svc.cases.current_watermark("C1"))

# 停机恢复：意见期限、协助催办、复核工作按持久化状态继续
svc.recover()

# 授权解释：证据链、使用史、程序当前有效性
svc.account_queries.explain("C1", no, "d")
```

`contracts/domain.schema.json` 定义领域资料结构，`fixtures/domain.json` 提供
不含真实身份信息的示例，`context.py` 负责读取并检查这些资料。

## 开发命令

- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译检查：`python3 -m compileall -q src tests`

上述命令只读取仓库内资料，不需要连接外部业务服务。

# 国家队梯队出场议事库

记录梯队资格、训练负荷、比赛样本、对手分析、医疗限制、教练观察、培养目标、阵容方案、回避关系与赛后反馈，支撑“按比赛当日可获得信息”的出场评议，并防止复盘被结果倒推。

## 设计原则

- **证据不被结论覆盖**：资格期、负荷、样本、对手分析、医疗、教练观察、培养目标、回避关系是相互独立的证据线，各自带 `observed_at` 与置信度。
- **按当日信息评议**：锁定记录 `evidence_cutoff`，只采信该时点之前的证据；方案修订与赞成意见同样不得晚于截止线。
- **职责分离**：提出阵容的教练不能终审本人方案；`medical_restriction` / `medical_clearance` 只能由 `medical` 角色登记，解禁必须指向具体限制证据。
- **冲突与少数意见并存**：同场比赛的方案修订与意见全部追加保留；并行提交靠聚合版本号检出冲突，意见靠 `proposal_hash` 绑定内容，方案内容漂移即进入复议。
- **一次性锁定**：名额与出场次序在终审时锁定并生成回执；重复确认沿用原回执、不产生新事件。
- **快照不可变**：锁定时固化证据、意见、医疗状态快照；赛后证据（含赛后反馈）只能作为未来比赛重算的输入。
- **查询给事实链而非评分**：返回入选/替补/暂缓的证据链、仍被接受的不确定性、少数意见与训练承诺兑现状态。
- **批量复盘断点续算**：进度以事件流为游标，中断后重跑自动跳过已完成比赛，从第一个未处理比赛继续。

## 目录

- `contracts/domain.schema.json`：对象、事件、证据种类与载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/squad_deliberation/`
  - `contracts.py`：基础契约校验（不改写输入，问题稳定排序）。
  - `store.py`：JSONL 事件存储，事件标识幂等、聚合版本号乐观并发。
  - `service.py`：议事服务（证据、方案、意见、终审、复盘）。
  - `projections.py`：只读事实链投影与比赛级视图。
  - `errors.py` / `clock.py`：领域错误与带时区时间。
- `tests/`：契约与服务规则测试。
- `docs/domain.md`：领域对象、事件与上层服务语义。

## 典型流程

```python
from squad_deliberation import EventStore, DeliberationService, athlete_fact_chain
import json
from datetime import datetime

schema = json.load(open("contracts/domain.schema.json", encoding="utf-8"))
service = DeliberationService(EventStore(schema=schema))

# 1. 各条证据线独立登记（医疗证据只能 medical 角色登记）
service.record_evidence("elig-a1", evidence_kind="eligibility", subject_ref="a1",
                        observed_at="2026-01-01T00:00:00+08:00", source_role="admin",
                        content={"eligible_from": "2026-01-01T00:00:00+08:00",
                                 "eligible_until": "2026-12-31T00:00:00+08:00"})

# 2. 教练提交方案（多名教练可并行提交冲突修订）
proposal = service.submit_proposal(
    "p1", competition_ref="cup-final",
    roster=[{"slot": 1, "athlete_ref": "a1", "status": "starter"}],
    proposer_role="coach", proposer_ref="coach-li")

# 3. 独立复核角色签署；反对/弃权意见同样保留
service.sign_opinion("o1", proposal_id=proposal["event_id"],
                     reviewer_role="final_reviewer", reviewer_ref="zhao",
                     position="agree", reason="证据链完整")

# 4. 终审一次性锁定名额与次序；重复确认沿用原回执
receipt = service.confirm_lineup(
    "lock1", competition_ref="cup-final",
    reviewer_role="final_reviewer", reviewer_ref="zhao",
    evidence_cutoff="2026-09-26T12:00:00+08:00",
    decision_reason="按当日证据；对 a1 样本不足的不确定性已记录并接受")

# 5. 查询事实链（没有单一评分）
chain = athlete_fact_chain(service.store.all_events(), "a1", "cup-final",
                           now=datetime.now())

# 6. 批量复盘：中断后重跑同一列表，自动从未处理比赛继续
service.batch_review([
    {"event_id": "review-m1", "competition_ref": "m1", "outcome": {"result": "loss"}},
    {"event_id": "review-m2", "competition_ref": "m2", "outcome": {"result": "win"}},
])
```

事件存储可传入文件路径获得 JSONL 持久化（`EventStore("events.jsonl", schema=schema)`），重开进程自动重建索引；批量复盘因此天然支持跨进程续算。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m squad_deliberation.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

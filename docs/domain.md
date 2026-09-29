# 领域约定

记录梯队资格、阵容证据、教练意见和历史出场决定的领域事件。

聚合对象包括 `athlete_profile`、`selection_evidence`、`lineup_proposal`、`development_commitment`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件类型

| 事件 | 聚合 | 含义 |
| --- | --- | --- |
| `EVIDENCE_RECORDED` | selection_evidence | 一条按种类登记的证据，`evidence_kind` 区分资格期、训练负荷、比赛样本、对手分析、医疗限制、教练观察、培养目标、回避关系、赛后反馈 |
| `MEDICAL_CLEARANCE_GRANTED` / `MEDICAL_CLEARANCE_REVOKED` | athlete_profile | 医疗限制的解除与撤回；解除者角色必须是 `team_physician` 或 `medical_committee` |
| `PROPOSAL_SUBMITTED` | lineup_proposal | 教练提交的阵容方案（名额、出场次序、引用证据、被接受的不确定性） |
| `OPINION_SIGNED` | lineup_proposal | 会签意见，立场为 `support`/`object`/`abstain`，并行签署互不覆盖 |
| `RECUSAL_DECLARED` | lineup_proposal | 裁判/教练就某场比赛声明回避 |
| `LINEUP_LOCKED` | lineup_proposal | 终审锁定，附证据截止时点、决策理由、完整名额次序与回执号 |
| `RECONSIDERATION_OPENED` | lineup_proposal | 对已锁定回执的重复确认出现内容漂移，另开复议修订流 |
| `RECALCULATION_TRIGGERED` | lineup_proposal | 赛后新增证据触发**未来比赛**方案重算 |
| `COMMITMENT_RECORDED` / `COMMITMENT_FULFILLED` | development_commitment | 培养承诺登记与按期/逾期兑现 |
| `REVIEW_COMPLETED` | lineup_proposal | 批量复盘中单场完成的检查点 |

## 关键业务规则

- **证据截止（point-in-time）**：锁定时校验引用证据在 `evidence_cutoff` 之前已发生；赛后反馈不能倒灌当日决策。
- **终审分离**：提出阵容的教练不能终审自己的方案；终审角色限主教练或选拔委员会，且锁定前至少有一名提议人之外的教练 `support`。
- **医疗权限**：医疗限制只有医疗角色可以解除；解除被撤回或截止时点仍未解除，相关队员不能进入锁定阵容。
- **回避**：声明回避者不得会签或终审；存在回避关系的队员不能以同一 `doubles_pair` 配对双打，拆分为各自单打不受限。
- **并行与少数意见**：多名教练可就同一比赛并行提交互相冲突的方案与意见，全部作为事件保留，锁定后仍可在事实链中查到反对/弃权意见。
- **一次锁定**：名额与出场次序在确认时一次性写入快照。重复确认内容一致时沿用原回执、不产生新事件；`roster`、`evidence_cutoff` 或 `decision_reason` 漂移则开启复议流，原回执快照保持不变。
- **只增快照**：赛后新增数据只能通过 `RECALCULATION_TRIGGERED` 指向未来比赛开新方案流；任何事件都不改写已锁定比赛的决策快照。
- **承诺闭环**：承诺在 `due_at` 前兑现记 `on_time`，之后记 `late`，逾期未兑现在查询中显示 `overdue`；兑现结果不可二次改写。
- **可续跑复盘**：批量复盘按比赛顺序处理，每场完成后写入 `REVIEW_COMPLETED` 检查点；中断后重跑自动跳过已完成比赛，从首个未处理比赛继续（配合 JSONL 存储可跨进程恢复）。

## 查询语义

`athlete_decision_report` 不给单一评分，而是返回：

1. `disposition`：`starter` / `substitute` / `hold` / `not_in_roster`；
2. `fact_chain`：证据、医疗解除、提交、各方法律意见、锁定按时间排列的事实链；
3. `accepted_uncertainties`：锁定时被明确接受、仍未消除的不确定性；
4. `minority_opinions`：被保留的少数（反对/弃权）意见；
5. `commitments`：后续训练承诺的 pending / overdue / fulfilled（on_time/late）状态；
6. `snapshot.immutable`：该报告基于不可变的锁定快照；`future_recalculations` 仅指向未来重算。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；契约层只定义可稳定交换的基础事实。

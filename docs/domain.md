# 领域约定

记录梯队资格、阵容证据、教练意见和历史出场决定的领域事件。

聚合对象包括 `athlete_profile`、`selection_evidence`、`lineup_proposal`、`development_commitment`。事件类型包括 `EVIDENCE_RECORDED`、`PROPOSAL_SUBMITTED`、`OPINION_SIGNED`、`LINEUP_LOCKED`、`REVIEW_COMPLETED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 证据种类

不同证据种类是相互独立的证据线，复盘时不得用一句结论互相覆盖。`EVIDENCE_RECORDED.payload.evidence_kind` 取值：

- `eligibility`：资格期（可入选窗口、到期日）。
- `training_load`：训练负荷与恢复数据。
- `match_sample`：近期比赛样本（含对手与场次时间）。
- `opponent_analysis`：对手分析。
- `medical_restriction`：医疗限制；只能由 `medical` 角色登记。
- `medical_clearance`：医疗解禁；只能由 `medical` 角色登记，且载荷需携带 `restriction_ref` 指向被解除的限制。医疗限制只有相应角色能解除，教练或终审无权覆盖。
- `coach_observation`：教练观察；多名教练可并行登记相互冲突的观察。
- `development_goal`：培养目标。
- `matchup_recusal`：回避关系（如兼项、配对冲突），`subject_ref` 取对阵双方标识。
- `post_match_feedback`：赛后反馈，`observed_at` 不得早于比赛结束；只影响未来方案。
- `training_commitment` / `commitment_outcome`：训练承诺及其兑现结果，服务于事实链中的承诺追踪。

每条证据携带 `observed_at`（事实发生时间）与可选 `confidence`。事实链查询会继续展示被接受的不确定性，登记低置信度不等于该证据被忽略。

## 事件载荷

- `EVIDENCE_RECORDED`：`evidence_kind`、`subject_ref`、`observed_at`、`source_role`，以及可选的 `source_ref`、`confidence`、`content`、`restriction_ref`、`competition_ref`。
- `PROPOSAL_SUBMITTED`：`competition_ref`、`roster`（每条含 `slot`、`athlete_ref`、`status`），可选 `expected_version`。
- `OPINION_SIGNED`：`reviewer_role`、`position`（agree/oppose/abstain）、`proposal_hash`。
- `LINEUP_LOCKED`：`evidence_cutoff`、`decision_reason`。
- `REVIEW_COMPLETED`：`competition_ref`、`outcome`。

## 上层服务语义

基础契约只定义可稳定交换的事实；以下规则由上层服务（`squad_deliberation.service`）保证：

- **按比赛当日信息评议**：锁定事件记录 `evidence_cutoff`，投影只采信 `observed_at <= cutoff` 的证据；赛后新增证据绝不进入已锁定快照。
- **并行冲突与少数意见**：方案与意见以内容追加保存，不就地更新。多名教练可并行提交互相冲突的方案，反对/弃权意见与赞成意见同等保留。
- **职责分离**：提出阵容的教练（proposer）不能终审自己的方案；终审由独立复核角色完成。
- **内容漂移进入复议**：意见通过 `proposal_hash` 绑定具体方案内容；方案内容变化后哈希改变，旧意见不满足新版本，需要重新签署。
- **锁定即一次性确定**：名额与出场次序在 `LINEUP_LOCKED` 时一次性锁定，并返回回执；对同一方案重复确认沿用原回执，不产生新事件、不改变结果。
- **快照不可变**：锁定时固化决策快照（方案、采纳的证据、意见、回避检查、医疗状态）。赛后数据可触发未来方案重算，但不得改写已完赛比赛的决策快照。
- **事实链查询**：查询不返回单一评分，而是给出每名队员入选/替补/暂缓的证据链、仍被接受的不确定性，以及训练承诺是否按期兑现。
- **批量复盘断点续算**：复盘以比赛为单位推进并记录游标，中断后从未处理的比赛继续，不重复处理已完成项。

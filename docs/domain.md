# 领域约定

记录梯队资格、阵容证据、教练意见和历史出场决定的领域事件。

聚合对象包括`athlete_profile`、`selection_evidence`、`lineup_proposal`、`development_commitment`。事件类型包括`EVIDENCE_RECORDED`、`PROPOSAL_SUBMITTED`、`OPINION_SIGNED`、`LINEUP_LOCKED`、`REVIEW_COMPLETED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `PROPOSAL_SUBMITTED`：载荷还需包含 `competition_ref`, `roster`。
- `OPINION_SIGNED`：载荷还需包含 `reviewer_role`, `position`。
- `LINEUP_LOCKED`：载荷还需包含 `evidence_cutoff`, `decision_reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。

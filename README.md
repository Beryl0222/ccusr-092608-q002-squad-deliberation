# 国家队梯队出场议事库

记录梯队资格、训练负荷、比赛样本、对手分析、医疗限制、教练观察、培养目标、回避关系与赛后反馈，并把"谁在什么信息下提出、谁反对、终审锁定了什么"沉淀为不可变的决策快照。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/squad_deliberation/`
  - `contracts.py`：基础事件信封校验（时间、版本、载荷必填与枚举）。
  - `eventstore.py`：只增事件存储（内存 / JSONL），事件标识幂等、版本按聚合递增。
  - `service.py`：议事服务——证据截止、终审分离、医疗权限、回避、并行意见、锁定回执、复议、赛后重算、承诺、可续跑批量复盘。
  - `queries.py`：只读事实链查询（入选/替补/暂缓、被接受的不确定性、少数意见、承诺兑现）。
- `tests/`：契约测试与业务规则测试（`_fixtures.py` 提供完整议事流夹具）。
- `docs/domain.md`：领域对象、事件与规则语义。

## 核心规则

- 方案评议只按比赛当日（证据截止时点前）可获得的信息进行。
- 提出阵容的教练不能单独终审；医疗限制只能由队医或医务委员会解除。
- 多名教练可并行提交冲突方案与意见，反对/弃权的少数意见不被覆盖。
- 名额与出场次序确认时一次性锁定；重复确认沿用原回执，内容漂移进入复议新流。
- 赛后新增数据只能触发未来比赛的方案重算，已完成比赛的决策快照不可改写。
- 查询返回事实链而非单一评分；批量复盘中断后从首个未处理比赛继续。

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

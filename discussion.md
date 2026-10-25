# Reference Labels Discussion

记录截至 2026-10-24 已明确的共识，供之后继续讨论时查阅。

## 数据与标注的区别

- ABCD 的 `original` 是原始对话；`delexed` 是将部分具体信息替换为占位符、并附带 `targets` 等标注的对话版本。`delexed` 本身不是我们新建的 reference label。
- 原客服实际执行的操作及参数可从 `delexed.targets` 提取，作为候选参考；不能直接把历史操作视为 agent 在该时刻唯一正确的行为。
- `scenario.personal`、`scenario.order` 和 `scenario.product` 将分别用于构建模拟的账户、订单和商品数据。agent 应通过相应工具获取这些信息，以模拟企业客服流程并测试工具使用。

## Reference label 的设计前提

- Reference label 用于评估 agent 在当时可见的信息下是否作出合适的判断，而不是复写原客服对话。
- 我们自己的工具名称、输入参数及返回行为确定后，才能定稿“允许调用的工具和正确参数”这部分标签；ABCD 原有的操作名不自动等于我们最终的工具接口。
- 客户回复的标签应描述可检查的内容要求和限制，不要求唯一的标准措辞。具体要求需结合最终的 agent 行为和工具流程确定。
- 测试将覆盖两种切点：工具调用前，以及需要依据政策决定下一步的阶段。两种案例的具体 reference label 留到工具接口和测试条件明确后再创建。

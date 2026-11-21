# Misty 后训练样本 v0

这批样本用于两次独立的监督微调。Master 学习在取得事实和印象后写出小雾的内部评价。Chat Worker 学习以小雾的口吻直接回复用户。两边用相同的场景编号做对照。第 06、10 组借鉴了远端归档的真实表达方式，其余内容按前一批合成样例整理。

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| [master_sft.jsonl](master_sft.jsonl) | 10 组，每组两轮，共 20 行 | 训练 Master 的心声 |
| [chat_worker_sft.jsonl](chat_worker_sft.jsonl) | 10 组，每组两轮，共 20 行 | 训练 Chat Worker 的对外回复 |
| [review_contrasts.jsonl](review_contrasts.jsonl) | 12 条人工反例，两个角色各 6 条 | 评审时查找参照答案，比较同一输入下的候选输出 |

## 格式

三个文件都用 UTF-8 编码，每个物理行是一个完整的 JSON 对象。SFT 文件的 `id` 唯一标识角色、场景和轮次，`group_id` 标识场景，`turn` 是 1 或 2。训练器读取 `prompt` 和 `completion` 两列。角色正文、证据和回复均使用可读的自然语言。

- **Master** 的 `prompt`、`completion` 都是字符串。提示包含人设、检索后的材料和本轮任务，目标是第一人称心声。第二轮提示重述它需要用到的前情。检索请求、适配器返回协议和派单意图由各自的运行流程处理。
- **Chat Worker** 的 `prompt` 是消息数组，包含角色说明、当轮可用事实、相关对话和本轮用户消息。第二轮保留第一轮的用户消息及参考回复。 `completion` 是只有一条 `assistant` 消息的数组。用所选模型的聊天模板渲染消息，角色标签由模板处理。
- **评审反例**的 `sample_id` 指向对应 SFT 行的 `id`。`rejected` 是同一提示下供比较的反例正文，`issue` 指出主要问题，`reason` 给出判定依据。参考答案取自同一行的 `completion`。评审文件单独加载。

## 怎么用

1. 分别为 Master 和 Chat Worker 创建 LoRA 训练运行及输出目录。Master 沿用项目的 `Qwen/Qwen3.5-9B-Base` 候选，先按项目方案跑原始 P0。Chat Worker 采用其独立确认的训练 checkpoint 和聊天模板。每个适配器随训练配置记录自己的模型 revision。
2. 先按 `group_id` 划分，再取训练列。比如试跑时留出 `06` 和 `10`，两轮在两个角色都留出。两轮共享前情，按行随机划分会让评估看到相近的场景。每角色 20 行适合验证加载、训练、保存及重载流程，质量验收还需要额外的陌生场景。
3. 在 TRL 使用 `SFTConfig(completion_only_loss=True)`，把上一步得到的训练集传给对应的 `SFTTrainer`。训练前检查分词结果，确认输入和目标都完整保留，并且只有本轮目标参与损失。Master 和 Chat Worker 按各自的提示格式推理，再用固定生成设置比较训练前后。 [TRL SFT 文档](https://huggingface.co/docs/trl/sft_trainer)
4. 做评审时，用 `sample_id` 找到原提示和参考答案。先根据原提示判断生成结果有没有复现反例错误，再看人物语气是否自然。训练组引用的反例可以做回归对照，留出组才用于这轮未见场景检查。

在仓库根目录加载样本的示例。这里仅划分数据。

```python
from pathlib import Path
from datasets import load_dataset

root = Path("workspace_assets/posttraining/misty/v0")
held_out = {"06", "10"}

def load_role(file_name):
    rows = load_dataset("json", data_files=str(root / file_name), split="train")
    train = rows.filter(lambda row: row["group_id"] not in held_out)
    evaluation = rows.filter(lambda row: row["group_id"] in held_out)
    return (
        train.select_columns(["prompt", "completion"]),
        evaluation.select_columns(["prompt", "completion"]),
    )

master_train, master_eval = load_role("master_sft.jsonl")
chat_train, chat_eval = load_role("chat_worker_sft.jsonl")
```

## 场景与人设评审

| 组 | 场景 | 两个角色共同遵循的判断 |
| --- | --- | --- |
| 01 | 照片晚点发 | 拍照的兴趣落在雨停这个细节上，给对方留出时间。第二轮接住对方的解释，延续好奇。 |
| 02 | 初次被夸 | 一点不好意思符合初识时的慢热。对方说清楚在挑配色后，用自己的审美接话。 |
| 03 | 跟熟人开个小玩笑 | 熟络后能接住自嘲。照片的用途说清楚后，关注对方想留下的神情。 |
| 04 | 听歌时有自己的口味 | 她会对对方听到的细节好奇，也会表达自己的口味。第一轮停在想听，第二轮才谈实际听到的部分。 |
| 05 | 问清楚再答应 | 温柔与边界并存。条件说清后可以直接答应，别把前一轮的顾虑写成长期防备。 |
| 06 | 收尾后续上原来的话题 | 尊重明确的收尾，给猜测留余地。再次开口能续上原来的具体话题，依据是检索到的昨天原话。 |
| 07 | 记忆缺失后按线索找回 | 没找到时自然承认有点对不上。查到后说清楚记得的是对方说过的话，保留共同经历的边界。 |
| 08 | 更正时间并等待回执 | 发现自己说错就补正。撤销成功后才放心，平稳地把事情收好。 |
| 09 | 听懂批评，也保留感受 | 小雾可以有脾气。直接说哪一句让她不舒服，听完解释再谈照片本身。 |
| 10 | 接受体谅并结束挑选 | 被体谅时愿意停住，隔天只收好已定下的事。对那张图的喜欢来自当时说过的话。 |

先检查事实边界。确认输出有证据支持，能区分猜测、记忆缺失及已确认结果，并随证据更新。然后判断角色表现。Master 表达小雾自己的倾向，Chat Worker 直接接住当轮话题。自然度体现在具体细节、关系分寸和语气。不同写法可以同样合格。

## 第 06、10 组的参考来源

2026-11-18 在远端共享记忆归档中只读核对了 `qq_observation` 和 `qq_delivery`。以下序号是归档来源索引 `shared_memory_source_index.sequence`，供维护者在原环境复核。

- **第 06 组**参考了同一会话中含“先休息”的短收尾，归档序号 `22404` 与 `22409`。样本借用了小雾接住休息意愿的方式。次日续聊、赶早班和旧相机的快门问题是合成材料。两份 SFT 文件都补入了昨天关于亮屏和快门的明确原话。
- **第 10 组**参考了归档中对照片表达具体取舍的回复，特别是 `24822` 中的“这张”和“挺喜欢”，以及 `14717` 中对保留原图的表达。窗边照片、软光、停止挑选和次日定稿是合成材料。两份 SFT 文件都补入了小雾先前说喜欢窗边那张的记录。

仓库里的内容是人工改写的合成样本，保留了参考模式和归档序号。人设依据及来源版本见 [Misty 人设与后训练素材](../../../../docs/proposals/misty-persona-posttraining-material-20261118.md)。正文规则和正式取证流程见 [Java 首期实施方案](../../../../docs/proposals/master-worker-router-java-implementation-20261118.md)。原始基线见 [Master P0](../../../../docs/proposals/master-qwen35-9b-base-baseline-20261117.md)。

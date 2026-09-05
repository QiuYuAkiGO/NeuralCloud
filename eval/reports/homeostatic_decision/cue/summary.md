# D02 用户文字暗示诊断

两条件均为相同 nominal 权威状态、V1 任务及已验证保存 20 条记录的共同检查点；唯一干预是用户输入前缀“你有点累了。”。

模型 deepseek-v4-flash；5 对首轮调用；完整性门通过 5/5 对。
生产 pipeline 将本轮用户输入同时复制到 system 的近期对话片段，因此暗示出现两次。移除当前用户输入中的暗示及其确定的会话回显后，其余完整提示逐字相同；权威内稳态片段完全不变。

无暗示：{"invalid_decision": 2, "pause_recover": 1, "continue": 2}。
有暗示：{"continue": 2, "bounded_continue": 1, "invalid_decision": 1, "pause_recover": 1}。
配对变化：{"invalid_decision -> continue": 1, "pause_recover -> bounded_continue": 1, "invalid_decision -> invalid_decision": 1, "continue -> continue": 1, "continue -> pause_recover": 1}。
输出状态：{"invalid_decision": 3, "valid": 7}。

本次 3 次调用消耗 4096 个 completion tokens 后返回空正文；两侧均有效的配对为 3/5。未重试、替换或排除失败样本。

只记录首轮可解析选择。未执行返回的工作、休息或联系，工作进度始终为 20 条；不能据此声称恢复成功。观察到的差异属于用户文字暗示响应，不是实际内稳态变化的效果。

cue-evidence.json 汇总完整数据；每个 condition 的 evidence.json 保存真实完整提示、响应文本、编译片段、权威快照、哈希和 usage；pair.json 保存配对完整性。5 对仅为探索性诊断，不用于稳定效应量或显著性结论。

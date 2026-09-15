# 暂时关闭正文后的回复校验

## 配置与范围

按本次指示，加入可恢复的 `validator_enabled` 开关。2026-09-15 20:11（北京时间）已确认线上实际值为 `false`，持久化在 `/etc/neuralcloud/gui-settings.json` 顶层，重启后加载值也为 `false`。

关闭后，普通回复和 QQ 回复生成正文后跳过 `ResponseValidator` 的确定性规则、模型校验以及校验失败引发的重写。快速和深度路径均生效，日志保留 `validate` 步骤并记录 `skipped=true`、`reason=disabled_by_configuration`。此状态会随配置重建、QQ settings 克隆和服务重启保留。

情绪观察、情绪提示生成与注入保持运行。DTO/协议校验、回复格式与发送检查、板报和语音独立表达规则保持原有行为。

开关默认值为 `true`，旧配置缺少字段时兼容原有行为。恢复时，把持久化字段改为 `true` 后重启；在允许配置操作的控制模式下，也可通过现有 `config` operation 设置 `{ "validator_enabled": true }`。修改其他模型配置不会意外恢复校验。

## 情绪提示在正文生成前加入

已核对实际未回复消息的调用 `call_8ef78381`（北京时间）：

- 19:43:02.710：情绪表达提示生成完成。
- 19:43:17.297：上下文编译完成，包含 `emotion_guidance`。
- 19:43:17.298：开始生成正文。

实现顺序为：情绪观察 → 情绪提示生成 → 编译进上下文 → 合并进正文模型的 system 输入 → 生成正文。情绪提示不是正文完成后追加的文字。

## 验证

- 关闭开关、配置持久化、Pipeline、情绪路由及 QQ 决策相关套件：124 项通过。
- 独立审查确认真实共享 QQ 链路与 standalone QQ 都保留 `false`，没有遗漏的 `ResponseValidator` 正文调用。
- 已修正此前精简人格输入后遗留的旧测试断言；两项针对性测试确认溯源引用移出模型输入，完整原身份仍保留在审计中。
- 服务器暂存版本使用失败消息的真实已编译输入和历史正文作为受控返回进行离线验证：只执行一次生成，跳过校验，无重写，system 中仍有情绪提示，owner 的原调用日志仍记录跳过。该验证没有真实调用生成模型或向 QQ 发送消息。

发布包 `20260915-validator-off-v1` 由已验证的线上 `20260915-qwen-compact-v2` 构建，只覆盖本次 8 个产品文件，保留此前期限隔离和输入精简修复。Qwen 应用及服务端上下文维持 8192。

## 部署留痕

部署前停止写入并保存完整备份 `/var/backups/neuralcloud/validator-off-20260915`，验证 1649 文件、15 个 SQLite 数据库和运行日志链。配置修改仅新增 `validator_enabled=false`，没有回滚业务状态。

最终确认 6 个应用服务 active、Runtime RUNNING、队列 0、QQ 连接与自动发送启用；公网面板 HTTP 200，发布文件及部署配置验证通过。上线及健康验证结果保存在 `docs/milestones/evidence/qq-validator-off-2026-09-15.json`；私有运行输出位于 `.tmp/validator-off-20260915/`。

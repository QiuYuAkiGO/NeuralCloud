# 生产记忆：离线迁移与合成规模验证

新版由 `production_memory_enabled` 显式启用。生产记忆 SQLite 保存不可变经历、带版本认识、来源权限、使用回执、保留原因及撤销标记。QQ 和实际本地对话在启用时使用该索引；旧研究路径的条件与默认行为不改变。

## 准备一致性副本

只对已按现有停写/快照流程取得的副本执行迁移。不要复制正在写入的 SQLite 主文件后忽略 WAL，也不要将真实聊天内容、密钥或快照提交到仓库。迁移不会连接 QQ、调用模型或改写输入文件。

输入可以是一个 `CharacterPreset` JSON；也可以是显式清单，引用 preset 和可选的共享原文库：

```json
{
  "schema_version": "production-memory-snapshot-v1",
  "owner_id": "misty",
  "preset_id": "misty",
  "preset_file": "misty.json",
  "preset_sha256": "填写实际SHA256",
  "shared_database": "shared-memory.sqlite3",
  "shared_sha256": "填写实际SHA256",
  "tombstones": []
}
```

路径相对清单所在目录解析；校验和可省略，提供时必须匹配。撤销清单条目使用 `{"source_id":"原有ID","reason":"撤销原因"}`，`source_id` 必须能在迁入目标找到；不会通过昵称或文本猜测对象。带非空 WAL 的共享库输入会被拒绝，应先制作正确的一致性副本。

```powershell
python -m harness.memory.production_migration inspect C:/Snapshots/Misty/manifest.json
python -m harness.memory.production_migration migrate C:/Snapshots/Misty/manifest.json --destination C:/Migration/Misty/production.sqlite3
```

`inspect` 仅返回文件哈希、大小、身份、对象数量和缺失证据数量，不输出原话。`migrate` 拒绝把任何源文件当目标，核对 owner/preset、原 ID、旧解释全部字段、人格与日修订原文/历史账本、版本留存和引用完整性，并在结束时再次校验输入文件哈希未变化。旧亲密度等数值作为历史数据归档，不作为新的具身奖励。

旧情景的 `source_refs` 必须与真实来源的规范 ID 或原始记录 ID 精确对应，且原文可见范围覆盖旧情景范围。没有映射、映射歧义或权限不符的旧情景及其结论只保留历史，不进入事实召回；不会从昵称、文本相似度或 ID 前缀猜测来源。报告的 `legacy_lineage_resolved` / `legacy_lineage_unresolved` 分别列出完整映射和未完成映射数量。补齐原文会改变输入指纹，须使用新目标库重新迁移，不重新调用模型。

日常修订账本使用的独立证据键，必须由 `FrozenSource` 的原文哈希、owner/preset、作者、时间、权限和原始引用共同核实后，才建立到规范来源 ID 的映射。只拿到旧摘要或无法匹配原文的账本，不能补造映射。后台安装日常修订会精确读取账本指名的来源，因此不受前台每页 256 条摄取游标落后的影响。

## 中断、重跑与回退

来源页与消费游标在同一目标事务提交；认识修订、来源引用与处理回执也一起提交。相同输入重跑沿游标继续，已迁入的原文、修订和回执不重复计数。命令完成前不要把目标设为生产状态目录。不同输入内容不能复用既有不可变来源 ID；出现冲突应调查快照版本，不要清除游标跳过错误。

离线迁移在写入任何生产 schema、来源、解释或游标之前，先在目标 SQLite 的单次事务内持久保存输入指纹。指纹覆盖 owner/preset、preset 与共享库的实际内容 SHA256、排序后的撤销条目及原因；不把文件路径或清单排版当作身份。离线消费游标也按该内容指纹命名，因此同一封存快照复制到另一路径后可以续跑。

同路径替换快照、修改 preset、共享原文或撤销清单后复用旧目标，会立即报 `migration_snapshot_fingerprint_mismatch_use_new_destination`，不会改变目标数据或游标。此前没有输入绑定、但已有数据或迁移历史的旧目标会报 `migration_unbound_existing_data_use_new_destination`，必须使用新目标重新迁移，不能猜测它与当前来源相同。指纹落盘后、生产库初始化之前发生中断也可由同一快照恢复。本规则仅限制离线迁移 API，不改变实时共享库持续摄取新来源的行为；已有成功演练证据保留原样，新版演练使用全新目标。

旧共享原文库可能尚无 `shared_memory_source_index`。离线迁移对此使用封存副本的原始 SQLite `rowid` 有界分页，仍逐条验证原文、索引字段、owner 与 preset；不在输入库上建立新表或改写内容。源校验和必须始终保持一致。该兼容读取只用于离线副本，实时共享库仍需正常的显式索引升级。

生产认知服务启动时，会在两个记忆 worker 启动之前，按已经加载的权威 preset 恢复一次 SQL 投影。首次运行时启用生产模式、切换或重新加载 preset 也使用同一恢复入口；同一已加载实例的后续前台请求直接跳过，不反复全量扫描。若 preset 保存后 SQL 发布中断，重启会重新同步已保存的解释，不重跑模型。恢复失败保留 publication pending 并报告 `production_memory_recovery_failed:<错误类型>`；重试成功前不会启动新的学习 worker，健康状态中可查看恢复状态。

新库是独立目标。上线前先检查报告的 `complete=true`、`source_unchanged=true`、owner/preset 与预期相同，且迁移中未忽略来源错误。在源副本上完成只读对照后，再遵循全项目的唯一 writer 切换流程。该工具不自动修改服务配置、替换运行目录、重发 QQ 消息或恢复旧快照。

原文撤销/逻辑删除立即阻断该原文、同一独立事件的重复采集、精确映射的旧情景及依赖认识的模型读取，显式 pin 也不能覆盖权限或撤销。后续导入的同源副本继承撤销，重跑不会复活。不可变审计原文仍保留；物理擦除归档不属于本工具的逻辑删除能力。历史版本只能由 owner/operator 审计接口读取，不能作为公开对话的授权召回。

兼容投影以每次完整 preset 快照为当前状态：删除或回滚的旧解释立即退出召回，同 ID 修订会更新当前文本、证据映射和版本，首版不可变记录与完整解释历史保留。依赖认识另行冻结证据版本；同 ID 证据变化后，已有准备回执失效，后续合法解释再生成新的认识版本。QQ 原始上下文和准备回执也使用同一撤销检查，旧源文件中缺失的可选默认字段保持原始字段集合。

## 封存历史快照迁移与回滚演练

三个通用验收脚本现位于版本库的 `tools/production_acceptance/`：`bridge_production_snapshot.py`、`memory_drill.py` 和 `memory_cold_hint.py`。它们从本轮已验收的冻结脚本提升，原件和成功演练证据保持不变；仅增加可从任意工作目录定位当前仓库的导入入口，并要求桥接调用者明确给出 preset 相对路径，不默认选择某个人格。三个工具均可用 `--repo` 显式选择代码仓库；以下模块命令从仓库根目录执行，也可从其他目录以脚本绝对路径启动。输入快照和生成数据库不得加入版本库。所有 `-new` 目标必须尚不存在，重试失败演练时使用另一个新名字，保留失败现场。

提升前冻结原件的 SHA256（历史 Linux 报告仍对应这些版本；入口调整不追溯改写旧报告）：

- `bridge_production_snapshot.py`：`8749c8bd79b13b0154d462a8ba1204fb920ec0d989accb54747a2a84af7add0f`
- `memory_drill.py`：`34093e7637de49341d15e1b1ed748c3b75226fbb7e08f091f78c57cf32d0affb`
- `memory_cold_hint.py`：`c4441ad50dbf93dda0370c698892d5f46f0655341a1bb5a2ca29e269ee29c1ac`

```bash
python -m tools.production_acceptance.bridge_production_snapshot --snapshot .tmp/production-snapshot-final --destination .tmp/production-linux/historical-memory-input-new --owner-id misty --preset-id misty --preset-relative state/presets/misty.json
python -m tools.production_acceptance.memory_drill --manifest .tmp/production-linux/historical-memory-input-new/manifest.json --destination-dir .tmp/production-linux/memory-drill-new
```

桥接脚本验证旧全服务清单封印及 `writers_stopped=true`，仅复制清单内明确指定的 preset 和共享原文库，不读取配置、令牌或其他日志。新清单记录源快照时间和校验和。演练脚本再复制这两项输入，对比全新迁移、中断续跑、重复迁移的表级逻辑哈希、对象数、原 ID、来源映射和历史留存，并重新验证源文件及输入副本哈希未变。输出只含汇总数据，异常仅显示类型和安全错误码；网络、子进程及外部模型均不参与。

回滚演练在另一个 SQL 副本加入明确标记的两条合成经历和两条认识，撤销其中一条已 pin 的来源，回退到旧 preset 兼容投影，再在副本前滚恢复。验收要求不可变原文和修订历史不丢失、仍有效的新认识可达、被移除的兼容投影隐藏、撤销不会因 pin 或前滚复活。输出的 `rollback-bundle.json` 同时指向旧 preset 与保留新事实的 SQL；只恢复旧 preset 无法保留升级后的事实。这不证明旧版本软件能读取新 SQL，也不替代真正的服务发布回退。

2026 年 9 月 15 日封存副本已在本地及 Linux 隔离环境完成该演练。首次迁移发现旧库没有 `shared_memory_source_index`；修复仅增加前述离线只读分页，未改写源库。该副本结果为 SQL sources 2093（包含旧情景投影）、records 2187、认识修订 94、旧 ID/历史核验 584；375 条旧情景完成来源映射，9 条未完成映射保留为历史并退出事实召回。快照时间不代表当前线上状态；本轮演练没有暂停或接管线上 writer。

## 10 万 / 100 万合成规模测试

```powershell
python -m tools.benchmark_production_memory --destination C:/Benchmarks/memory-100k.sqlite3 --rows 100000 --batch-size 1000 --queries 100
python -m tools.benchmark_production_memory --destination C:/Benchmarks/memory-1m.sqlite3 --rows 1000000 --batch-size 1000 --queries 100
```

第二条需显式选择 100 万行；程序不会从 10 万自动扩展。默认拒绝覆盖已有目标，确认同一个合成测试库后可加 `--resume` 从原子游标继续。批量摄取每批一个事务，不逐条 commit。工具只生成固定格式合成记录，不读取生产目录。

Linux 使用同一工具，例如：

```bash
python -m tools.benchmark_production_memory --destination .tmp/production-linux/memory-hot-100k-new.sqlite3 --rows 100000 --queries 100
python -m tools.benchmark_production_memory --destination .tmp/production-linux/memory-hot-1m-new.sqlite3 --rows 1000000 --queries 100
```

报告给出摄取时间、文件大小、预热后的 SQL 召回 p50/p95/max、候选数及硬预算 192。词项倒排索引按受众分区并限制每词候选，人物检索单独限制 64 条；JSON 解码发生在候选截断之后。

默认还在独立 SQL 副本测量实际生产本地准备链：`PipelineCognitiveAdapter.prepare_response → CognitiveSharedMemory.local_context → production.prepare → _ask`。该路径包含权限检索、增量同步、当前来源摄取、检索和展示回执、证据绑定、生产上下文预算、结构化输出 schema 与最终消息构造；在模型客户端入口截停，不产生模型回答或准备提交。生产本地路径本身不调用旧 `ContextCompiler`，因此不能以调用旧编译器或单独 `json.dumps` 冒充这一测量。报告 `production_context_preparation.warm_prepare_ms` 给出真实准备 p50/p95/max，首轮单列 `first_prepare_ms`；`--sql-only` 可显式跳过这一部分。

准备测量使用新建应用对象和数据库副本，副本保留以便检查，原合成库不会混入演练来源或回执。Python 模块和操作系统页缓存可能已经预热，首轮数字不等同于冷进程或冷磁盘；不会执行 `drop_caches`，也不得为测试清除现网缓存。网络、embedding、模型、应用启动、HTTP 传输、QQ gate/body 上下文及回复提交均不计入此项，因此不能将合成的本地准备结果当作真实历史负载、完整 QQ 延迟或冷启动验收。

## Linux 文件级缓存回收提示

独立 `memory_cold_hint.py` 模式只接受 `/tmp/neuralcloud-isolated-acceptance/` 或 `/var/tmp/neuralcloud-isolated-acceptance/` 下的全新子目录，没有读取已有数据库的参数。它自行生成合成 SQL 及实际生产准备副本，核对路径、文件身份、单硬链接、所有者和 SQLite 文件头。仅对这些文件执行 `fsync` 后的 `os.posix_fadvise(..., POSIX_FADV_DONTNEED)`，不访问 `/var/lib` 或线上数据库，不执行系统级 `drop_caches`。

```bash
python -m tools.production_acceptance.memory_cold_hint --destination-dir /tmp/neuralcloud-isolated-acceptance/cold-100k-new --rows 100000 --samples 20
python -m tools.production_acceptance.memory_cold_hint --destination-dir /tmp/neuralcloud-isolated-acceptance/cold-1m-new --rows 1000000 --samples 20
```

报告保存为输出目录内 `cold-hint-report.json`。`cold_hint_recall` 和 `cold_hint_production_prepare` 分别保留每个样本的耗时、候选/入模条数及 `/proc/self/io` 的 `read_bytes` 增量；提示耗时单列，不混入检索/准备耗时。准备路径与热测试相同，模型入口截停；前五次应用初始化样本另行排除，之后每次样本均重新提示回收自身数据库页面。热测试中的两个默认空采样回调仅供这一独立模式使用，不改变默认计时或执行行为。

内核可以忽略提示，因此必须称为“缓存回收提示后的样本”，不能宣称绝对冷缓存。正的 `read_bytes` 增量证明该进程发生了存储读取，不证明所有页面都已清除；零增量尤其不能证明冷缓存，内存文件系统也可能始终为零。Python 对象、模块、非数据库缓存保持加载，进程范围 I/O 还可能包含其余线程的读取；报告不属于冷进程启动或 QQ 端到端延迟。

非 Linux 平台正常运行返回 `status=unsupported` 且不创建文件。下面的本地接线测试显式模拟提示及 I/O 计数，只验证实际准备路径、计数窗口和路径拒绝，不能用于性能验收：

```bash
python -m tools.production_acceptance.memory_drill --self-test --destination-dir .tmp/memory-drill-selftest-new
python -m tools.production_acceptance.memory_cold_hint --self-test --destination-dir .tmp/memory-cold-hint-selftest-new
python -m pytest tests/test_production_memory_migration.py tests/test_production_acceptance_tools.py -q
```

迁移、旧 schema 断点恢复、输入指纹保护和 benchmark 回归共 16 项已通过，包含现代索引与旧 rowid 两种来源的 A→B 同路径替换拒绝、目标字节不变及原快照恢复续跑。Linux 冷/热性能数字以各次保存的实际报告为准。脚本可用或模拟测试通过不等于满足 300 ms 性能目标。

版本库工具提升后，以上 16 项与 2 项桥接 CLI 回归共 18 项通过；新增回归覆盖从仓库外目录调用、显式 preset 选择、源哈希不变、已有目标和篡改输入拒绝。迁移/回滚自检与 30 行、3 样本的缓存提示模拟自检也从仓库外目录运行通过；Windows 普通调用仍返回 `unsupported` 且不创建目标。该接线验证没有重跑 10 万/100 万性能，也不改变此前 Linux 实测结论。

迁移与兼容桥接仍会在启动/后台遍历旧 preset DTO；前台查询不因此恢复全量旧召回。旧同化 worker 的彻底退役属于后续兼容切换，不能把本工具或单项 benchmark 宣称为完整生产上线。

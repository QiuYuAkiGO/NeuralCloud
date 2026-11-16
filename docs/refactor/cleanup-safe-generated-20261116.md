# 明确可清理项：历史 pytest 临时状态（2026-11-16）

已从 Git 索引移除并实际删除 665 个旧评测生成文件，已加入精确忽略规则；本次未提交、推送或改动服务器。

## 范围及理由

- engine 的 pytest 临时用例目录：91 个文件。
- runtime 的历史 attempt 临时用例目录：287 个文件。
- runtime 的另一批 pytest 临时用例目录：287 个文件。
- 合计 8,048,228 字节（约 7.68 MiB）；这是已删除文件的逻辑体量，未测量文件系统实际可用空间变化，Git 历史未重写。
- 每个文件的路径、哈希、大小、处置理由与恢复基线见 [逐文件记录](cleanup-safe-generated-20261116.json)。

这些文件位于 `pytest-temp*/test_*/` 下，是使用 `tmp_path` 的测试生成的 SQLite、状态快照、journal、锁和合成输出；测试源码仍在，运行脚本通过 `--basetemp` 创建临时目录并读取独立 JUnit 汇总，不以这些留存状态作为输入。

范围引用核查中，历史全仓 inventory 仅记录原始路径，engine 的 validation.json 仅记录当时的 `--basetemp` 命令；两者均保留原样。当前保留清单与现场源码来源清单均不包含本批移除项。

## 保留的证据与恢复

engine-evidence.json、engine-summary.md、validation.json，以及 runtime 各批次的 coverage.json、junit.xml、probes.json 均保留；不触碰旧测试源码、业务实现、未跟踪试验目录或数据库运行目录。

所有原文件可从基线提交 `9f33e57e` 恢复，本机原路径的文件已删除；需要历史副本时，优先在独立 worktree 检出该提交，避免覆盖后续修改。只恢复跟踪且尚未提交本批删除时，可使用 `git restore --staged --pathspec-from-file=<本批路径清单>`。

## 执行边界

首次直接删除被自动审批检查拒绝，先完成了仅移除 Git 跟踪；用户随后明确授权实际删除，已通过 `tools/cleanup/remove-verified-pytest-output.ps1 -Execute` 完成删除，并确认 665 个目标全部不存在。

脚本先校验清单哈希、全部目标路径、祖先目录无链接以及每个文件大小与哈希，再逐文件删除，不递归删除目录；不带 `-Execute` 仅校验，重复执行允许文件已经不存在。执行结果见 [回执](cleanup-safe-generated-20261116-execution.json)。原始清单保留首次索引清理时的 action 字段以固定哈希，最终状态以此回执为准。

本批属于生成产物清理，不代表其余未列入保留清单的旧代码可以删除；其它源码的部署、恢复与额外使用方仍需逐项核对。

# Lean Inspector

按目标准备编译工件并提取报告。目标可以包含登记的 D5 与 Reg 模块。

```sh
make lean-report-prepare LEAN_TARGETS="D5.S0.Diagonal.PigeonholeFiber"
make lean-report-scoped LEAN_TARGETS="D5.S0.Diagonal.PigeonholeFiber"
```

定向报告包含选中目标、所需 utility 输入及 import 闭包的声明、公理和材料。报告 JSON 与摘要、输入证明、来源记录及材料 ZIP 一起发布。PR 和 push 的检查按变更选择目标并读取本次定向报告。

Lake 将每个模块的原生报告及 trace 保存在 `.lake/build/lean-inspector/modules`。模块报告绑定报告格式、编译闭包和 utility 输入。生产时校验模块报告，复用时由 Lake trace 判断是否需要重提取。定向聚合保留实际生产来源和模块材料。

独立缓存生产使用 `make lean-cache-prepare FULL_BUILD=1`，准备编译工件、审计程序和登记模块的原生报告。显式设置 `REGENERATE_REPORT_ARTIFACTS=1` 可重提取模块报告，保留已有编译工件。Actions 和 Release 快照运输编译工件与模块报告，恢复后由 Lake 核对当前输入。

[缓存与检查契约](../../docs/develop/spec/golden-ledger-repo-spec.md#a22-ci-cache-flow)

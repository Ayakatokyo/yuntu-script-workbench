# yuntu-script-workbench

本仓是独立云图脚本工作台。取数与分析运行代码迁移自 `yuntu-meta-skill` 已提交源码，后续只在本仓维护；运行时不得导入兄弟仓文件。客户原始数据、视频、模型、凭证、运行产物和测试临时目录不入 Git 或正式 ZIP。

开工核对本仓分支、HEAD、现有差异，阅读 SKILL.md、README.md 和 packaging.json。业务代码、依赖及测试在本仓修改；每次实际优化按工作区规则同步项目管理唯一进度和总规划。使用 `python3 -B tools/test.py` 和 `python3 -B tools/build.py`；打包前审视正式入口与完整运行闭包。不要修改 `yuce-plugin`。

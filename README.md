# 云图素材脚本工作台

稳定 ID：`yuntu-script-workbench`。本仓独立交付云图榜单与详情取数、视频音轨准备、Agent 音频理解结果导入、第一阶段脚本报告及第二阶段用户自定义分析。本地 faster-whisper 模型链路已移除；平台文件理解工具只能由 Agent 调用，普通 Python 脚本不直接调用模型。

- 用户流程：[SKILL.md](SKILL.md)
- 取数契约：[references/collection-contract.md](references/collection-contract.md)
- 脚本契约：[references/script-contract.md](references/script-contract.md)
- CLI：`python3 scripts/run.py --help`；原子采集仍由 `scripts/collector.py` 执行
- 测试：`python3 -B tools/test.py`
- 构建：`python3 -B tools/build.py`，正式文件以 [packaging.json](packaging.json) 为准

`acquire` 默认在调用方当前目录的 `云图素材脚本数据/run-.../` 建立本次运行目录，完成采集并把缺失平台脚本的视频音轨保存到 `audio/by-video/`；抽取临时文件放在音频目标目录，避免跨设备移动。`transcripts/manifest.json` 列出待 Agent 理解的音频；Agent 将完整工具回复和纯转写文本各存为 UTF-8 文件，使用 `import-transcript --video-sha ... --response-file ... --text-file ...` 导入。识别后仍无可用文本时，使用 `mark-unavailable --video-sha ... --response-file ... --review-note ...` 记录可追溯缺口；未经处理的音频仍阻断 `finalize-scripts`。最终报告的筛选区展示平台品牌范围；榜单和公式页共享基于已核验品牌事实 ID 的标题规则分类筛选，不把该分类称为平台权威品牌归属。完整榜单不按标题品牌词自动裁剪。`finalize-scripts` 重建事实和固定五分区的离线 `script-report/index.html`：素材筛选条件、完整素材榜单、脚本内容公式、单素材脚本、脚本片段库。榜单自定义指标使用云图元技能式分组弹窗、默认列、确定/取消和本地持久化；公式一级柱状图联动二级环形图，单素材核心数据与内容公式沿用云图元技能的字段默认值、选择交互和来源文本定位；缺失数据保持空态。结构数据及收据继续保存并绑定来源。同一视频 SHA 去重，平台原文始终优先，转写无可靠时间码。

运行数据、客户视频与音频、转写结果、凭证和模型服务回复不入 Git 或 ZIP。旧运行目录中的 `asr/` 文件作为历史数据保留，不由新版流程继续生成或改写。在线 RPA、平台音频工具及目标平台安装须分别实测；本地离线测试不代表这些步骤已验收。

总体规划和当前进度见[项目管理](../../项目管理/docs/AI短视频Skill开发批次进度.md)。

第二阶段默认分区按本轮 manifest 解析；新结论用 `field_refs` 声明实际证据字段，原话用 `quotes` 绑定逐字来源。正文可用而分段缺失不再误拦截，旧结果仍按原保守规则核验。详见脚本契约。

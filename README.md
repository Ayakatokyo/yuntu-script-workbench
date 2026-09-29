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

## 第二阶段宿主报告交付（2026-09-29）

分析准备继续使用 `handoff-index.json` 的完整事实分页和 `result-template.json`；按业务问题读取所需页，保留原文、字段来源和哈希，复制模板到新结果文件再填写。`workflow --analysis-result` 先校验结构化结果与证据，仅写 `host-report-handoff.json`，返回 `deliveryStatus=awaiting_host_report`；这不表示最终报告已交付。

当前对话宿主从已核验事实、原业务问题和必要的指标口径出发独立分析并组织报告。结构化结果用于审计重点主张，不规定报告的章节、结论数量、图表或叙事顺序；报告不必逐条复述结果卡。新增事实与计算须引用可核对的原字段和来源，推断、假设和建议须明确标注；需要更多同轮事实时使用现有读取或重新选择路径，不重新采集。按用户需求直接交付对话文本、Markdown 或 HTML；只有选择文件交付时才登记本地回执。

```bash
python "$SKILL_ROOT/scripts/run.py" analysis-page --analysis-root "$ANALYSIS_ROOT" --page 1
python "$SKILL_ROOT/scripts/run.py" analysis-status --analysis-root "$ANALYSIS_ROOT"
# 文件交付示例：把 report.md 放在 analysis-report/revisions/0001/ 后登记
python "$SKILL_ROOT/scripts/run.py" register-analysis-report --analysis-root "$ANALYSIS_ROOT" --revision 0001 --format markdown
# 同一分析下修订：保留 0001，在 revisions/0002/ 写新文件后登记
python "$SKILL_ROOT/scripts/run.py" register-analysis-report --analysis-root "$ANALYSIS_ROOT" --revision 0002 --format markdown
python "$SKILL_ROOT/scripts/run.py" analysis-status --analysis-root "$ANALYSIS_ROOT"
```

登记前重新核对分析结果、来源及交接哈希；文件交付支持 `index.html`、`report.md` 或 `report.txt`，分别用 `--format html|markdown|text`。登记时检查正文非空、HTML/CSS/Markdown 本地资源可用且不越出该版目录，记录输入及每个输出文件 SHA-256。`deliveryStatus=completed`、`reportVerified=true` 表示本地文件通过完整性校验，不代替事实解释、视觉观感或用户验收。展示修改在同一分析下新增递增的四位 revision，旧文件和旧收据保持不变；涉及新事实的实质修订先用既有事实读取与选择路径核验，并更新相应审计结果或建立新分析，无需重跑采集。旧版无 revision 的宿主 HTML 可继续校验。历史固定报告的 v1 收据只读核验，标为 `legacy_completed`。阶段一固定素材汇总报告不变。宿主报告制作耗时单独记录。

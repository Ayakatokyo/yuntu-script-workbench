# 云图素材脚本工作台

稳定 ID：`yuntu-script-workbench`。本仓独立交付云图榜单与详情取数、视频音轨准备、Agent 音频理解结果导入、第一阶段脚本报告及第二阶段用户自定义分析。本地 faster-whisper 模型链路已移除；平台文件理解工具只能由 Agent 调用，普通 Python 脚本不直接调用模型。

- 用户流程：[SKILL.md](SKILL.md)
- 取数契约：[references/collection-contract.md](references/collection-contract.md)
- 脚本契约：[references/script-contract.md](references/script-contract.md)
- CLI：`python3 scripts/run.py --help`；原子采集仍由 `scripts/collector.py` 执行
- 测试：`python3 -B tools/test.py`
- 构建：`python3 -B tools/build.py`，正式文件以 [packaging.json](packaging.json) 为准

`acquire` 默认在调用方当前目录的 `云图素材脚本数据/run-.../` 建立本次运行目录，完成采集并把缺失平台脚本的视频音轨保存到 `audio/by-video/`；抽取临时文件放在音频目标目录，避免跨设备移动。`transcripts/manifest.json` 列出待 Agent 理解的音频；Agent 将完整工具回复和纯转写文本各存为 UTF-8 文件，使用 `import-transcript --video-sha ... --response-file ... --text-file ...` 导入。识别后仍无可用文本时，使用 `mark-unavailable --video-sha ... --response-file ... --review-note ...` 记录可追溯缺口；未经处理的音频仍阻断 `finalize-scripts`。最终 `script-report/index.html` 是只展示已获详情素材的单页脚本工作台：折叠展示取数与选材条件、内容公式分布，主体并列素材列表、核心数据与完整正文、当前素材内容公式。一级柱图保持单行；二级环图和图例居中并按网格排列。页面不显示全量/详情按钮、素材榜单、素材搜索/来源筛选；分布图下方设独立全宽的“内容公式与原句”区块，按一二级公式筛选详情素材和逐字原句，点击可定位完整脚本；一二级公式分区显示，原句每页四条、桌面按 2×2 排列并可翻页。品牌分类筛选继续基于已核验事实 ID，属于标题规则分类，不视为平台权威品牌归属。完整榜单及详情事实仍保存在 `report.json` 和事实清单中，供来源校验与第二阶段使用。同一视频 SHA 去重，平台原文始终优先，转写无可靠时间码。

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

执行流程增加 `describe-analysis` 字段概览、`analysis-page --end-page` 连续分页及只读 `check-analysis-result`；预检不替代正式发布或报告验收。使用方式与边界见 SKILL 和输出契约。

云图新增 `import-transcripts --batch-file` 共享批量导入扫描、`status --summary` 紧凑读取；workflow 续跑复用已保存业务问题并拒绝问题冲突。原单条导入和完整状态输出保留。

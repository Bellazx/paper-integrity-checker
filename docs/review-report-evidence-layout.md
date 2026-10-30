# 复核报告：逐问题附图（2026-10-23）

后续中英文复核报告都按“问题说明 → 对应核心截图 → 图注 → 下一问题”排版。
已交付的张礼知三篇报告及压缩包不重生成。

每个维度的确认问题在 `image_review`、`data_review`、`ref_review`、
`methodology_review` 中以（1）（2）（3）独立分段。`evidence_images` 和
`excel_evidence_regions` 必须指定 `dimension` 和整数 `issue_index`，编号对应
该维度的问题编号。同一问题允许多图；图片列表的先后次序不决定对应关系。
多个问题缺少明确映射时，校验要求修正，不自动猜测配图。单一问题兼容旧格式。

```json
{
  "data_review": "（1）Table 1的具体问题。\n\n（2）Fig.2的具体问题。",
  "evidence_images": [
    {"dimension": "data", "issue_index": 1, "path": "/path/table1.png", "caption": "Table 1核心证据"},
    {"dimension": "data", "issue_index": 2, "path": "/path/fig2.png", "caption": "Fig.2核心证据"}
  ]
}
```

中英文共享截图收集器和版式函数，保持图片比例、图注间距以及明确的图文对应关系。
经复核明确选取的截图不再被统一截断为四张，`max_height` 等版式元数据会被保留。
自动检测截图仍是定位候选，不能代替复核确认。

中文正式结论用“有风险／未发现风险”；英文用“Risk identified／No risk identified”。
英文按问题和图注分别翻译，保留原问题编号和所有证据，任何必需字段缺失都不能静默省略。
英文结论由已核验的内部判定确定，翻译模型不重新决定风险。正文不追述“原报告”“初审报告”
或其英文对应措辞。数据库及内部判定枚举保持不变。

代码入口：`modules/review_report_layout.py`、`modules/review_public_wording.py`、
`modules/english_report_generator.py`、`api/services/review_validation.py`。
实际 workflow 渲染器位于 `/home/library/.claude/skills/paper-batch-review/scripts/generate_review_report.py`，
本地 `skills/paper-batch-review/scripts/generate_review_report.py` 的逐问题排版调用也已同步。
API 的提示词、截图预校验和确定性结果生成同步提供问题编号。

验证：`tests/test_review_inline_evidence.py` 覆盖 API/workflow 中文 PDF、英文 PDF、
打乱截图输入顺序、多图对应一个问题、缺失映射、六张明确截图不丢失、翻译缺字段失败。
PDF 检查包含实际图文顺序、图片比例及图文无重叠；翻译服务在测试中被替代，不调用网络。

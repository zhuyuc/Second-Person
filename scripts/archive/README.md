# 归档脚本

本目录存放开发阶段的一次性冒烟 / 验收脚本，不属于持续维护的运维工具。

持续维护的脚本保留在 `scripts/` 根目录：

- `calibrate_retrieval.py` — 检索参数离线校准
- `calibrate_followup_window.py` — followup 窗口校准
- `_regression_message_flow.py` — 消息流回归（需运行中服务）
- `_regression_proposal_confirm.py` — proposal 确认回归

归档脚本可在本地手动运行，但不纳入 CI 默认门禁。

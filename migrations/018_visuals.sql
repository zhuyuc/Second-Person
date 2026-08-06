-- 018: 图形能力持久化
-- conversations 表新增 visuals 列，存储 tool_visual 事件产出的图形 JSON
-- 刷新后历史消息可恢复 FlowChartSVG / MermaidChart 渲染
ALTER TABLE conversations
ADD COLUMN visuals TEXT;
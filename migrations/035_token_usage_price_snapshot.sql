-- token_usage 增加单价快照与金额列：
-- 用量发生时冻结当时配置的单价并折算金额，此后修改单价不影响历史金额；
-- cost 为 NULL 表示该条用量未配单价（查询时按当前单价兜底，未配不计入）。
ALTER TABLE token_usage
ADD COLUMN input_price REAL;
ALTER TABLE token_usage
ADD COLUMN output_price REAL;
ALTER TABLE token_usage
ADD COLUMN cost REAL;
-- 历史数据回填：按模型当前配置单价补齐快照与金额（与现行实时折算口径一致，
-- 回填后即冻结，后续调价不再追溯）。Provider 已删除或单价未配置的行保持 NULL，
-- 费用查询时按当时最新单价兜底。
UPDATE token_usage
SET input_price = (
        SELECT p.input_price
        FROM providers p
        WHERE p.model_id = token_usage.model_name
    ),
    output_price = (
        SELECT p.output_price
        FROM providers p
        WHERE p.model_id = token_usage.model_name
    )
WHERE input_price IS NULL;
UPDATE token_usage
SET cost = input_tokens / 1000000.0 * COALESCE(input_price, 0) + output_tokens / 1000000.0 * COALESCE(output_price, 0)
WHERE cost IS NULL
    AND (
        input_price IS NOT NULL
        OR output_price IS NOT NULL
    );
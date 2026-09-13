-- Provider 增加用途模态（文本/图片/视频），与 API 协议 provider_type 解耦。
-- token_usage 增加媒体计费口径：按张 / 按秒，费用仍以 cost 冻结。

ALTER TABLE providers ADD COLUMN modality TEXT NOT NULL DEFAULT 'text';

UPDATE providers
SET modality = 'video'
WHERE lower(provider_type) = 'comfyui'
  AND lower(COALESCE(model_id, '')) LIKE '%wan%';

UPDATE providers
SET modality = 'image'
WHERE lower(provider_type) = 'comfyui'
  AND modality = 'text';

ALTER TABLE token_usage ADD COLUMN usage_kind TEXT NOT NULL DEFAULT 'token';
ALTER TABLE token_usage ADD COLUMN quantity REAL;

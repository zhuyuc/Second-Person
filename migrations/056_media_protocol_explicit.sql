-- 视频不再借用文本协议。已保存的云端视频按当时实际厂商改协议，之后只认下拉里的选择。

UPDATE providers
SET provider_type = 'dashscope'
WHERE lower(COALESCE(modality, 'text')) = 'video'
  AND lower(provider_type) IN ('openai_compatible', 'anthropic', 'custom')
  AND (
    lower(COALESCE(base_url, '')) LIKE '%dashscope%'
    OR lower(COALESCE(base_url, '')) LIKE '%maas.aliyuncs.com%'
    OR lower(COALESCE(model_id, '')) LIKE 'wan%'
    OR lower(COALESCE(model_id, '')) LIKE '%wanx%'
  );

UPDATE providers
SET provider_type = 'kling'
WHERE lower(COALESCE(modality, 'text')) = 'video'
  AND lower(provider_type) IN ('openai_compatible', 'anthropic', 'custom');

-- 图片的 Anthropic 没有独立实现，旧数据按当时实际走的 OpenAI 图片接口保留。
UPDATE providers
SET provider_type = 'openai_compatible'
WHERE lower(COALESCE(modality, 'text')) = 'image'
  AND lower(provider_type) = 'anthropic';

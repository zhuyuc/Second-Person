-- 007: raw_docs 新增 extracted_text 列
-- 缓存图片（VLM/OCR）或文档解析出的正文，供 --recompile / 被动重提炼复用，
-- 避免对同一张图片重复调用视觉模型（成本控制）。
ALTER TABLE raw_docs
ADD COLUMN extracted_text TEXT;
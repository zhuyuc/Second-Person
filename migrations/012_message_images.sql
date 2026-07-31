-- 对话图片持久化：user 消息携带的图片存 data/chat_images/，
-- 此列记 JSON 数组（文件名列表），历史加载时回传可访问 URL
ALTER TABLE conversations
ADD COLUMN images TEXT;
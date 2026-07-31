from sentence_transformers.util import cos_sim
from sentence_transformers import SentenceTransformer
import os
import time

os.environ['HF_HOME'] = r'D:\project\Second-Person\embedding\models'


print("=" * 50)
print("BGE-M3 完整功能验证")
print("=" * 50)

# 加载模型(第二次加载,从本地缓存直接读,几秒即可)
print("\n[1] 加载模型...")
t0 = time.time()
model = SentenceTransformer('BAAI/bge-m3', device='cuda')
print(f"    加载耗时 {time.time()-t0:.1f} 秒")

# 投资场景测试文本
texts = [
    "茅台的护城河来自品牌和渠道",
    "白酒行业竞争格局稳定,头部集中度高",
    "宁德时代动力电池全球市占率第一",
    "新能源汽车产业链利润向下游转移"
]

# 编码
print(f"\n[2] 编码 {len(texts)} 段文本...")
t0 = time.time()
embeddings = model.encode(texts, normalize_embeddings=True)
elapsed_ms = (time.time()-t0) * 1000
print(f"    编码耗时 {elapsed_ms:.0f} 毫秒")
print(f"    向量维度: {embeddings.shape}")

# 相似度对比
print("\n[3] 语义相似度对比:")
print(
    f"    茅台 vs 白酒行业: {cos_sim(embeddings[0], embeddings[1]).item():.3f}  (应较高)")
print(
    f"    茅台 vs 宁德时代: {cos_sim(embeddings[0], embeddings[2]).item():.3f}  (应较低)")
print(
    f"    宁德时代 vs 新能源: {cos_sim(embeddings[2], embeddings[3]).item():.3f}  (应较高)")
print(
    f"    茅台 vs 新能源: {cos_sim(embeddings[0], embeddings[3]).item():.3f}  (应最低)")

# 判定
sim_related_1 = cos_sim(embeddings[0], embeddings[1]).item()
sim_unrelated = cos_sim(embeddings[0], embeddings[3]).item()

print("\n" + "=" * 50)
if sim_related_1 > sim_unrelated + 0.1:
    print("✅ 验证通过!BGE-M3 模型工作完全正常")
    print("   语义相关的文本相似度显著高于不相关文本")
else:
    print("❌ 异常,请检查环境")
print("=" * 50)

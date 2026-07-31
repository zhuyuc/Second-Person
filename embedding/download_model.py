"""下载 BGE-M3 模型到 embedding/models/（setup 脚本会自动调用，也可手动运行）。"""
import os
from pathlib import Path

# 模型缓存固定在项目内 embedding/models/，与 serve.py 的 HF_HOME 保持一致
MODELS_DIR = Path(__file__).parent / "models"
os.environ['HF_HOME'] = str(MODELS_DIR)
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

from sentence_transformers import SentenceTransformer  # noqa: E402
import torch  # noqa: E402

# 有 CUDA 用 CUDA，否则回退 CPU（与 serve.py 的设备选择逻辑一致）
device = "cuda" if torch.cuda.is_available() else "cpu"

print("=" * 50)
print("开始下载 BGE-M3 模型")
print(f"下载位置:{MODELS_DIR}")
print(f"镜像源:{os.environ['HF_ENDPOINT']}")
print(f"加载设备:{device}")
print("模型大小:约 2.3GB,请耐心等待")
print("=" * 50)

model = SentenceTransformer('BAAI/bge-m3', device=device)

print("\n✅ 模型下载并加载成功!")
print(f"向量维度:{model.get_sentence_embedding_dimension()}")
print(f"模型文件已保存到:{MODELS_DIR / 'hub'}")

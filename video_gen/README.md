# 本地文生视频（ComfyUI + Wan 2.1 T2V 1.3B）

本目录仅放视频部署说明与下载缓存。**引擎复用** `../image_gen/comfyui/`，不另起第二套 ComfyUI。

## 目录

```text
video_gen/
├── README.md          # 本文件
└── _downloads/        # 可选下载缓存（gitignore）

image_gen/comfyui/     # 共用引擎
workflows/wan21_t2v_1_3b.json
data/chat_videos/genv_*.mp4
```

## 安装模型

```powershell
powershell -ExecutionPolicy Bypass -File D:\project\Second-Person\video_gen\setup_local.ps1
```

国内可先设镜像：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
powershell -ExecutionPolicy Bypass -File D:\project\Second-Person\video_gen\setup_local.ps1
```

放到 ComfyUI 对应目录（便携包常见为 `ComfyUI/models/`）：

| 文件 | 典型目录 |
| --- | --- |
| `wan2.1_t2v_1.3B_fp16.safetensors` | `diffusion_models/` |
| `wan_2.1_vae.safetensors` | `vae/` |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | `text_encoders/` |

也可改用 GGUF 量化版，并把设置页 `model_id` / 工作流 UNET 节点改为对应文件名。

来源可参考 ComfyUI 官方 Wan 文档与 Hugging Face `Comfy-Org/Wan_2.1_ComfyUI_repackaged`。

## 启动

与文生图相同：

```powershell
cd D:\project\Second-Person\image_gen\comfyui
.\run_nvidia_gpu.bat
```

建议空闲显存；生视频前关闭向日葵 / ToDesk。浏览器打开 http://127.0.0.1:8188 ，导入 `workflows/wan21_t2v_1_3b.json` 手动跑通一条 480p ~3s。

## Second Person 配置

应用启动时会自动：
1. 若 `video_gen` 槽位未绑定，幂等创建/绑定 Wan ComfyUI Provider（复用文生图的 `base_url`）
2. 启动预热队列探测 ComfyUI 连通性（`image_gen` / `video_gen`）

手动重配：

1. 设置页 → Provider 类型选 **ComfyUI（本地文生图/视频）**
2. Base URL：`http://127.0.0.1:8188`
3. 模型 ID：Wan 权重文件名（如 `wan2.1_t2v_1.3B_fp16.safetensors`）
4. 任务-模型分配 →「文生视频模型（本地 ComfyUI）」绑到该 Provider（可与文生图共用同一地址、不同 model_id）
5. 对话中说「生成一段橘猫在窗台晒太阳的短视频」触发 `generate_video`

## 注意

- V1：一次 1 条、约 3 秒、480p；常见耗时数分钟；超时默认 600s
- 同轮不要既出图又出视频
- 不要把权重或 `chat_videos/` 提交进 git
- 不要把 ComfyUI `output/` 或 `:8188` 暴露给前端

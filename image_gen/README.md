# 本地文生图（ComfyUI + SDXL）

本目录是 Second Person 的本地生图根。引擎与大模型放在 `comfyui/`，**不进 git**。

## 目录

```text
image_gen/
├── README.md          # 本文件
├── setup_local.ps1    # 下载完成后的解压/配置/启动脚本
└── comfyui/           # ComfyUI 便携包（gitignore）
    ├── run_nvidia_gpu.bat
    ├── ComfyUI/models/checkpoints/sd_xl_base_1.0.safetensors
    └── output/
```

工作流 JSON：`../workflows/sdxl_txt2img.json`  
对话最终展示图：`../data/chat_images/gen_*.png`

## 安装

1. 下载 ComfyUI Windows 便携包：https://github.com/Comfy-Org/ComfyUI/releases
2. 解压到 `image_gen/comfyui/`（目录内应能直接看到 `run_nvidia_gpu.bat`）
3. 下载 SDXL base 到：

```text
image_gen/comfyui/ComfyUI/models/checkpoints/sd_xl_base_1.0.safetensors
```

https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/blob/main/sd_xl_base_1.0.safetensors  
国内可用 hf-mirror / ModelScope 同源文件。

## 启动

```powershell
cd D:\project\Second-Person\image_gen\comfyui
.\run_nvidia_gpu.bat
```

或（无 pause，适合后台）：

```powershell
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --listen 127.0.0.1 --port 8188
```

浏览器打开 http://127.0.0.1:8188 ，用 `1024x1024`、每次 1 张验收。

## Second Person 配置

本机已写入 Provider `prov_008`（类型 `comfyui`）并绑定 `image_gen` 槽位。若需重配：

1. 设置页 → 添加 Provider：
   - 类型：`ComfyUI（本地文生图）`
   - Base URL：`http://127.0.0.1:8188`
   - 模型 ID：`sd_xl_base_1.0.safetensors`
   - API Key：可留空
2. 任务-模型分配 →「文生图（本地 ComfyUI）」选中该 Provider
3. 对话中说「画一只趴在窗台的橘猫」触发 `generate_image`

## 注意

- 生图前尽量关闭向日葵 / ToDesk 等远控
- 不要把 `comfyui/` 或 `_downloads/` 提交进 git
- 不要把 ComfyUI `output/` 暴露给前端
- ComfyUI 需保持运行；重启机器后先开 8188，再聊
- 文生视频共用本引擎，见 `../video_gen/README.md` 与 `../workflows/wan21_t2v_1_3b.json`

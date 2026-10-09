# 本地 System One（jev-style）

情绪快路径的本地决策引擎。协议兼容 TypeSafe Jev 的 `POST /v1/systemone`，
由 Second Person 的 `system_one` Provider 调用。

## 目录

```text
system_one/                    # 与 embedding/ 同思路
├── README.md
├── setup_local.ps1            # 建 venv + 下权重 + 写 config + 绑槽位
├── write_service_config.py
├── ensure_provider.py
├── hf.env                     # HF_HOME / HF_ENDPOINT（gitignore，setup 生成）
├── venv/                      # gitignore：隔离依赖
└── models/                    # gitignore：HF 权重缓存（≈1.5GB / 0.8b）
```

## 安装

```powershell
powershell -ExecutionPolicy Bypass -File D:\project\Second-Person\system_one\setup_local.ps1
```

国内可先设：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
```

## 启动

默认 **CUDA，随主程序一起后台拉起**（`lazy: false`，`wait: false` 不堵启动）。
本地生图/记忆检索前会临时停服让出 GPU，之后情绪识别再按需拉起。

也可手动：

```powershell
.\system_one\venv\Scripts\jev-style.exe serve --host 127.0.0.1 --port 8765 --release 0.8b --device cuda
```

健康检查：`http://127.0.0.1:8765/v1/models` 或 `/health`。

## Second Person 配置（自动）

`setup_local.ps1` 与应用启动时会幂等：

1. 创建 Provider（类型 `system_one`，`http://127.0.0.1:8765`，模型 `0.8b`）
2. 将 **情绪快路径模型** 槽绑到该 Provider（setup 用 `--force-bind`；日常启动仅在槽位为空时绑定，不覆盖你手改）

`start.py` 检测到 `system_one/venv` 后会**随主程序拉起** jev-style（optional，失败不挡主站；`stop --all` 才一起停）。

手动补绑：

```powershell
python system_one\ensure_provider.py --force-bind
```

情绪标签为七情：`joy/anger/sorrow/fear/love/disgust/desire` + `neutral`。

## 注意

- 不要把 `venv/`、`models/` 提交进 git
- 不要把 `:8765` 暴露到公网
- 与 ComfyUI 同机：靠「按需加载 + 生图前释放」共享 8GB，不要同时常驻两个 CUDA 模型
- 首次情绪调用若冷启动超过 `mood_fast_path_timeout_ms` 会短暂降级词典，热起来后走 GPU

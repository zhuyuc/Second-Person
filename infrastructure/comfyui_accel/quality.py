"""无损档质量门禁与 ComfyUI 启动参数（不改采样数学路径）。"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# 跳步缓存 / 有损加速节点（lossless 模式禁止出现在工作流中）
LOSSLESS_FORBIDDEN_CLASS_TYPES: frozenset[str] = frozenset({
    "MagCache",
    "WanVideoMagCache",
    "TeaCache",
    "WanVideoTeaCache",
    "TeaCacheKJ",
    "ApplyTeaCache",
    "ApplyMagCache",
    "ModelSamplingTeaCache",
})

# 主路径禁止的量化权重后缀（lossless）
LOSSLESS_FORBIDDEN_MODEL_SUFFIXES: tuple[str, ...] = (".gguf",)

# 可识别的文本编码器 / CLIP 加载节点（用于 T5→CPU）
_CLIP_LOADER_CLASSES: frozenset[str] = frozenset({
    "CLIPLoader",
    "DualCLIPLoader",
    "TripleCLIPLoader",
    "CLIPLoaderGGUF",
    "WanVideoTextEncoderLoader",
})

# BlockSwap 参数节点（若工作流含 Wrapper 节点则可标定）
_BLOCK_SWAP_CLASSES: frozenset[str] = frozenset({
    "WanVideoBlockSwap",
    "Wan22BlockSwap",
})


def normalize_quality_mode(value: Any, default: str = "lossless") -> str:
    raw = str(value or default).strip().lower()
    if raw in ("lossless", "lossy"):
        return raw
    return default


def _iter_nodes(wf: Mapping[str, Any]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for nid, node in wf.items():
        if isinstance(node, dict) and "class_type" in node:
            out.append((str(nid), node))
    return out


def assert_lossless_workflow(wf: Mapping[str, Any]) -> None:
    """工作流含有损节点时抛错。"""
    bad: list[str] = []
    for nid, node in _iter_nodes(wf):
        ctype = str(node.get("class_type") or "")
        if ctype in LOSSLESS_FORBIDDEN_CLASS_TYPES:
            bad.append(f"{nid}:{ctype}")
    if bad:
        raise RuntimeError(
            "当前为无损质量模式（comfyui_quality_mode=lossless），"
            f"工作流含禁止的有损加速节点：{', '.join(bad)}。"
            "请改用 workflows/wan21_t2v_1_3b_lossless.json，"
            "或将 comfyui_quality_mode 设为 lossy（会降低输出质量）。"
        )


def assert_lossless_model_id(model_id: str) -> None:
    name = (model_id or "").strip().lower()
    if not name:
        return
    for suf in LOSSLESS_FORBIDDEN_MODEL_SUFFIXES:
        if name.endswith(suf):
            raise RuntimeError(
                "当前为无损质量模式，禁止使用量化权重 "
                f"（model_id={model_id!r}）。"
                "请改用 fp16/safetensors（如 wan2.1_t2v_1.3B_fp16.safetensors），"
                "或将 comfyui_quality_mode 设为 lossy。"
            )


def assert_lossless_generation(
    wf: Mapping[str, Any],
    *,
    model_id: str = "",
    quality_mode: str = "lossless",
) -> None:
    if normalize_quality_mode(quality_mode) != "lossless":
        return
    assert_lossless_workflow(wf)
    assert_lossless_model_id(model_id)


def apply_lossless_workflow_patches(
    wf: dict[str, Any],
    *,
    t5_on_cpu: bool = True,
    block_swap: int = 0,
) -> dict[str, Any]:
    """就地补丁：T5/CLIP→CPU；若存在 BlockSwap 节点则写入块数。"""
    for _nid, node in _iter_nodes(wf):
        ctype = str(node.get("class_type") or "")
        inputs = node.setdefault("inputs", {})
        if t5_on_cpu and ctype in _CLIP_LOADER_CLASSES:
            # 原生 CLIPLoader 用 device=cpu；Wrapper 文本加载器常见 load_device
            if "device" in inputs or ctype == "CLIPLoader":
                inputs["device"] = "cpu"
            if "load_device" in inputs:
                inputs["load_device"] = "cpu"
            if "precision" in inputs and ctype == "WanVideoTextEncoderLoader":
                # 保持原 precision，仅确保不强制上 GPU
                pass
        if ctype in _BLOCK_SWAP_CLASSES and "blocks_to_swap" in inputs:
            inputs["blocks_to_swap"] = max(0, int(block_swap))
    return wf


def build_comfyui_launch_extras(
    *,
    lowvram: bool = True,
    use_sage_attention: bool = True,
    reserve_vram_gb: float = 0.0,
    extra_args: str | Sequence[str] | None = None,
) -> list[str]:
    """由配置拼出 ComfyUI 追加启动参数（去重由 merge 负责）。"""
    extras: list[str] = []
    if lowvram:
        extras.append("--lowvram")
    if use_sage_attention:
        extras.append("--use-sage-attention")
    try:
        reserve = float(reserve_vram_gb or 0)
    except (TypeError, ValueError):
        reserve = 0.0
    if reserve > 0:
        extras.extend(["--reserve-vram", str(reserve)])
    if isinstance(extra_args, str) and extra_args.strip():
        extras.extend(extra_args.split())
    elif isinstance(extra_args, (list, tuple)):
        extras.extend(str(x) for x in extra_args if str(x).strip())
    return extras


def merge_launch_args(
    command: str | Iterable[Any] | None,
    extras: Sequence[str],
) -> str | list[str] | None:
    """把 extras 合并进已有 command；已存在的 flag 不重复添加。"""
    if command is None:
        return None
    extra_list = [str(x) for x in extras if str(x).strip()]
    if not extra_list:
        return list(command) if not isinstance(command, str) else command

    if isinstance(command, str):
        existing = set(command.split())
        to_add: list[str] = []
        skip_next = False
        i = 0
        while i < len(extra_list):
            tok = extra_list[i]
            if skip_next:
                skip_next = False
                i += 1
                continue
            if tok in existing:
                # --reserve-vram N：若已有 flag，跳过其后数值
                if tok == "--reserve-vram" and i + 1 < len(extra_list):
                    skip_next = True
                i += 1
                continue
            to_add.append(tok)
            if tok == "--reserve-vram" and i + 1 < len(extra_list):
                to_add.append(extra_list[i + 1])
                skip_next = True
            i += 1
        if not to_add:
            return command
        return (command.rstrip() + " " + " ".join(to_add)).strip()

    cmd = [str(x) for x in command]
    existing = set(cmd)
    i = 0
    while i < len(extra_list):
        tok = extra_list[i]
        if tok in existing:
            if tok == "--reserve-vram" and i + 1 < len(extra_list):
                i += 2
            else:
                i += 1
            continue
        cmd.append(tok)
        existing.add(tok)
        if tok == "--reserve-vram" and i + 1 < len(extra_list):
            cmd.append(extra_list[i + 1])
            existing.add(extra_list[i + 1])
            i += 2
        else:
            i += 1
    return cmd


def launch_extras_from_config(config: Any) -> list[str]:
    """从 ConfigManager 读无损启动参数。"""
    get = getattr(config, "get", None)
    get_raw = getattr(config, "get_raw", None)
    if not callable(get):
        return build_comfyui_launch_extras()
    mode = normalize_quality_mode(get("comfyui_quality_mode", "lossless"))
    # lossy 档仍可选用启动加速，但不强制
    lowvram = bool(get("comfyui_lowvram", True))
    sage = bool(get("comfyui_use_sage_attention", True))
    reserve = float(get("comfyui_reserve_vram_gb", 0) or 0)
    extra = ""
    if callable(get_raw):
        extra = get_raw("comfyui_launch_extra_args", "") or ""
    extras = build_comfyui_launch_extras(
        lowvram=lowvram,
        use_sage_attention=sage,
        reserve_vram_gb=reserve,
        extra_args=extra,
    )
    if mode == "lossy" and not sage and not lowvram and not extra:
        return []
    return extras

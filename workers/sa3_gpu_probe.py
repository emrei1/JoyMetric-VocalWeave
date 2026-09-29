from __future__ import annotations
import torch
import flash_attn
from flash_attn import flash_attn_func
import stable_audio_3  # noqa: F401


def main():
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable")
    print("torch:", torch.__version__)
    print("cuda:", torch.version.cuda)
    print("gpu:", torch.cuda.get_device_name(0))
    print("compute capability:", torch.cuda.get_device_capability(0))
    print("arch list:", torch.cuda.get_arch_list())
    print("flash_attn:", getattr(flash_attn, "__version__", "unknown"))
    if torch.cuda.get_device_capability(0) == (12, 0) and "sm_120" not in torch.cuda.get_arch_list():
        raise RuntimeError("PyTorch build has no sm_120 support")
    q = torch.randn(1, 64, 4, 64, device="cuda", dtype=torch.float16)
    with torch.inference_mode():
        y = flash_attn_func(q, q, q, dropout_p=0.0, causal=False)
    torch.cuda.synchronize()
    if not torch.isfinite(y).all():
        raise RuntimeError("FlashAttention kernel returned non-finite values")
    print("GPU_PROBE_OK")


if __name__ == "__main__":
    main()

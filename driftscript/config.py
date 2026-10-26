"""
Configuration, Whisper model memory profiles (fast / normal / heavy),
and real-time GPU VRAM / system RAM / CPU AVX2 diagnostics.
"""

import configparser
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("driftscript.config")

# Base directories
CACHE_DIR = Path.home() / ".driftscript_cache"
LEGACY_CUTSCRIPT_CACHE_DIR = Path.home() / ".obs_transcriber_cache"
DRIFT_CONF_PATH = Path.home() / ".config" / "CutWire Drift" / "CutWire Drift.conf"
DEFAULT_DRIFT_MCP_URL = os.environ.get("DRIFT_MCP_URL", "http://127.0.0.1:4731/mcp")
DEFAULT_SERVER_HOST = os.environ.get("DRIFTSCRIPT_HOST", "127.0.0.1")
DEFAULT_SERVER_PORT = int(os.environ.get("DRIFTSCRIPT_PORT", "4732"))

# Three explicit Whisper model tiers based on GPU memory footprint
WHISPER_PROFILES: Dict[str, Dict[str, Any]] = {
    "fast": {
        "id": "fast",
        "label": "Fast (Rápido)",
        "whisper_model": "small",
        "legacy_cache_models": ["small", "base", "tiny"],
        "vram_estimate_mb": 1200,
        "vram_estimate_gb": 1.2,
        "vram_display": "~1.2 GB VRAM",
        "ram_fallback_display": "~1.8 GB RAM",
        "batch_size": 8,
        "compute_type_cuda": "float16",
        "description": "Transcrição ultrarrápida com baixo uso de memória (~1.2 GB VRAM). Ideal para rascunhos rápidos e máquinas com pouca memória livre.",
    },
    "normal": {
        "id": "normal",
        "label": "Normal (Equilibrado)",
        "whisper_model": "medium",
        "legacy_cache_models": ["medium"],
        "vram_estimate_mb": 2800,
        "vram_estimate_gb": 2.8,
        "vram_display": "~2.8 GB VRAM",
        "ram_fallback_display": "~3.8 GB RAM",
        "batch_size": 4,
        "compute_type_cuda": "float16",
        "description": "Excelente equilíbrio entre precisão em português e consumo moderado de memória (~2.8 GB VRAM).",
    },
    "heavy": {
        "id": "heavy",
        "label": "Heavy (Máxima Precisão)",
        "whisper_model": "large-v3",
        "legacy_cache_models": ["large-v3", "large", "large-v2"],
        "vram_estimate_mb": 4800,
        "vram_estimate_gb": 4.8,
        "vram_display": "~4.8 GB VRAM",
        "ram_fallback_display": "~6.5 GB RAM",
        "batch_size": 2,
        "compute_type_cuda": "float16",
        "description": "Máxima precisão lexical para termos técnicos, nomes próprios e fala rápida (~4.8 GB VRAM).",
    },
}


def check_cpu_capabilities() -> Dict[str, Any]:
    """Check CPU model and whether AVX / AVX2 instructions are supported."""
    model_name = "Unknown CPU"
    flags = set()
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("model name") and model_name == "Unknown CPU":
                    model_name = line.split(":", 1)[1].strip()
                elif line.startswith("flags"):
                    flags.update(line.split(":", 1)[1].strip().split())
    except Exception:
        pass

    return {
        "cpu_model": model_name,
        "has_avx": "avx" in flags,
        "has_avx2": "avx2" in flags,
        "has_fma": "fma" in flags,
        "avx2_safe_mode": "avx2" not in flags,
    }


def get_system_ram_info() -> Dict[str, Any]:
    """Read /proc/meminfo for system RAM metrics in MB and GB."""
    mem_total_kb = 0
    mem_avail_kb = 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail_kb = int(line.split()[1])
    except Exception:
        pass

    total_mb = round(mem_total_kb / 1024, 1)
    free_mb = round(mem_avail_kb / 1024, 1)
    used_mb = round(max(0.0, total_mb - free_mb), 1)
    return {
        "total_mb": total_mb,
        "used_mb": used_mb,
        "free_mb": free_mb,
        "total_gb": round(total_mb / 1024, 2),
        "used_gb": round(used_mb / 1024, 2),
        "free_gb": round(free_mb / 1024, 2),
        "percent": round((used_mb / total_mb * 100), 1) if total_mb > 0 else 0.0,
    }


def get_gpu_vram_info() -> Dict[str, Any]:
    """
    Query GPU VRAM status via nvidia-smi and torch.cuda (when available).
    Never triggers AVX2 CPU instructions.
    """
    info: Dict[str, Any] = {
        "available": False,
        "device_name": None,
        "total_mb": 0,
        "used_mb": 0,
        "free_mb": 0,
        "total_gb": 0.0,
        "used_gb": 0.0,
        "free_gb": 0.0,
        "percent": 0.0,
        "torch_allocated_mb": 0,
        "torch_reserved_mb": 0,
        "temperature_c": None,
    }

    try:
        res = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            first_line = res.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_line.split(",")]
            if len(parts) >= 4:
                total_mb = float(parts[1])
                used_mb = float(parts[2])
                free_mb = float(parts[3])
                temp_c = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else None
                info.update(
                    {
                        "available": True,
                        "device_name": parts[0],
                        "total_mb": round(total_mb),
                        "used_mb": round(used_mb),
                        "free_mb": round(free_mb),
                        "total_gb": round(total_mb / 1024, 2),
                        "used_gb": round(used_mb / 1024, 2),
                        "free_gb": round(free_mb / 1024, 2),
                        "percent": round((used_mb / total_mb * 100), 1) if total_mb > 0 else 0.0,
                        "temperature_c": temp_c,
                    }
                )
    except Exception:
        pass

    try:
        import torch

        if torch.cuda.is_available():
            info["available"] = True
            if not info["device_name"]:
                props = torch.cuda.get_device_properties(0)
                info["device_name"] = props.name
                total_mb = props.total_memory / (1024**2)
                free_b, total_b = torch.cuda.mem_get_info(0)
                free_mb = free_b / (1024**2)
                used_mb = total_mb - free_mb
                info.update(
                    {
                        "total_mb": round(total_mb),
                        "used_mb": round(used_mb),
                        "free_mb": round(free_mb),
                        "total_gb": round(total_mb / 1024, 2),
                        "used_gb": round(used_mb / 1024, 2),
                        "free_gb": round(free_mb / 1024, 2),
                        "percent": round((used_mb / total_mb * 100), 1) if total_mb > 0 else 0.0,
                    }
                )
            info["torch_allocated_mb"] = round(torch.cuda.memory_allocated(0) / (1024**2), 1)
            info["torch_reserved_mb"] = round(torch.cuda.memory_reserved(0) / (1024**2), 1)
    except Exception:
        pass

    return info


def read_drift_mcp_token() -> Optional[str]:
    """
    Read the local CutWire Drift MCP token from ~/.config/CutWire Drift/CutWire Drift.conf.
    Never logs or prints the token.
    """
    if os.environ.get("DRIFT_MCP_TOKEN"):
        return os.environ["DRIFT_MCP_TOKEN"].strip()

    if not DRIFT_CONF_PATH.exists():
        return None

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(DRIFT_CONF_PATH, encoding="utf-8")
        if parser.has_section("mcp") and parser.has_option("mcp", "token"):
            token = parser.get("mcp", "token").strip().strip('"').strip("'")
            return token or None
    except Exception:
        pass
    return None

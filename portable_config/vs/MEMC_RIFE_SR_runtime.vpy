### 2026 super-resolution + 2025/2026 RIFE NV runtime combination.
### State is intentionally independent from MEMC_RIFE_NV_runtime.vpy.

import json
import os

import vapoursynth as vs
from vapoursynth import core
import k7sfunc as k7f


DEFAULT_CONFIG = {"sr_preset": "uai_rtx_default", "rife_model": 46}
RIFE_MODELS = {46, 4151, 422, 4221, 4251, 426, 4262, 47, 48, 49}


def make_preset(model, h_pre, rife_h_pre, res_opt, h_max=2160, st_eng=True, opt_lv=5, cuda_opt=None):
    return {
        "model": model,
        "h_pre": h_pre,
        "fp16_qnt": True,
        "gpu": 0,
        "gpu_t": 2,
        "st_eng": st_eng,
        "res_opt": res_opt,
        "res_max": list(res_opt) if st_eng else [1920, 1200],
        "ws_size": 0,
        "h_max": h_max,
        "lk_fmt": False,
        "opt_lv": opt_lv,
        "cuda_opt": [1, 0, 0] if cuda_opt is None else cuda_opt,
        "rife_h_pre": rife_h_pre,
    }


# Values below are copied from the approved 2026 static VPy presets. JSON only
# chooses an id, so TensorRT tuning stays fixed and cannot be user-modified.
SR_PRESETS = {
    "uai_rtx_default": make_preset(
        "the_database_AnimeJaNaiV2L1_x2_fp16_op17.onnx",
        720,
        1440,
        [1280, 720],
        h_max=1440,
        st_eng=False,
        opt_lv=3,
        cuda_opt=[0, 0, 0],
    ),
    "animejanai_v31_balanced": make_preset(
        "2x_AnimeJaNai_HD_V3.1_Balanced_SPANF3_b8f64_unshuffle_fp16.onnx", 1080, 2160, [1920, 1080]
    ),
    "animejanai_v31_balanced_sharp": make_preset(
        "2x_AnimeJaNai_HD_V3.1Sharp1_Balanced_SPANF3_b8f64_unshuffle_fp16.onnx", 1080, 2160, [1920, 1080]
    ),
    "animejanai_v31_performance": make_preset(
        "2x_AnimeJaNai_HD_V3.1_Performance_SPANF3_b5f48_unshuffle_fp16.onnx", 1080, 2160, [1920, 1080]
    ),
    "animejanai_v31_performance_sharp": make_preset(
        "2x_AnimeJaNai_HD_V3.1Sharp1_Performance_SPANF3_b5f48_unshuffle_fp16.onnx", 1080, 2160, [1920, 1080]
    ),
    "starsample_1x": make_preset("1x_StarSample_V2.0_Lite_NS_fp16.onnx", 1080, 1080, [1920, 1080]),
}

for _height, _width, _rife_height in ((540, 960, 2160), (720, 1280, 2160), (1080, 1920, 2160)):
    SR_PRESETS["realesrgan_x4v3_%d" % _height] = make_preset(
        "realesr-general-x4v3_dynamic.onnx", _height, _rife_height, [_width, _height]
    )

for _height, _width, _rife_height in ((540, 960, 1080), (720, 1280, 1440), (1080, 1920, 2160)):
    SR_PRESETS["liveaction_span_%d" % _height] = make_preset(
        "2xLiveActionV1_SPAN_490000.onnx", _height, _rife_height, [_width, _height]
    )
    SR_PRESETS["starsample_2x_%d" % _height] = make_preset(
        "2x_StarSample_V2.0_Lite_fp16.onnx", _height, _rife_height, [_width, _height]
    )


def portable_config_dir():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def config_path():
    return os.path.join(portable_config_dir(), "script-opts", "rife_sr_runtime.json")


def normalize_config(data):
    if not isinstance(data, dict) or set(data) != {"sr_preset", "rife_model"}:
        return dict(DEFAULT_CONFIG)
    sr_preset = data.get("sr_preset")
    rife_model = data.get("rife_model")
    if (
        not isinstance(sr_preset, str)
        or sr_preset not in SR_PRESETS
        or type(rife_model) is not int
        or rife_model not in RIFE_MODELS
    ):
        return dict(DEFAULT_CONFIG)
    return {"sr_preset": sr_preset, "rife_model": rife_model}


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8-sig") as file:
            return normalize_config(json.load(file))
    except (OSError, ValueError, TypeError):
        return dict(DEFAULT_CONFIG)


config = load_config()
preset = SR_PRESETS[config["sr_preset"]]
rife_model = config["rife_model"]
turbo, flow_scale = (1, 0.25) if rife_model == 46 else (2, 1.0)

core.num_threads = k7f.vs_t_dft
clip = video_in

# Super-resolution is complete before FPS conversion and RIFE interpolation.
ret = k7f.FMT_CTRL(clip, h_max=1200, h_ret=True)
clip = k7f.FMT_CTRL(clip, h_max=preset["h_pre"], fmt_pix=1 if preset["lk_fmt"] else 0)
clip = k7f.UAI_NV_TRT(
    clip,
    crc=False,
    model_pth=preset["model"],
    opt_lv=preset["opt_lv"],
    cuda_opt=preset["cuda_opt"],
    int8_qnt=False,
    fp16_qnt=preset["fp16_qnt"],
    gpu=preset["gpu"],
    gpu_t=preset["gpu_t"],
    st_eng=preset["st_eng"],
    res_opt=preset["res_opt"],
    res_max=preset["res_max"],
    ws_size=preset["ws_size"],
)
clip = k7f.FMT_CTRL(clip, h_max=preset["h_max"], fmt_pix=1 if preset["lk_fmt"] else 0)

ret = k7f.FPS_CTRL(clip, fps_in=container_fps, fps_ret=True)
clip = k7f.FMT_CTRL(clip, h_max=preset["rife_h_pre"], fmt_pix=0)
clip = k7f.RIFE_NV(
    clip,
    model=rife_model,
    int8_qnt=False,
    turbo=turbo,
    fps_in=container_fps,
    fps_num=2,
    fps_den=1,
    sc_mode=1,
    gpu=0,
    gpu_t=2,
    ws_size=0,
    flow_scale=flow_scale,
)
clip.set_output()

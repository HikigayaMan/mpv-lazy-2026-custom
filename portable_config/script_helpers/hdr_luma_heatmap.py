#!/usr/bin/env python3
"""Generate relative luma heatmaps from an mpv raw screenshot.

The helper intentionally uses only the Python standard library so it works with
the bundled mpv-lazy Python runtime. It accepts the PPM file written by
hdr_luma_heatmap.lua and writes two PNGs: a continuous false-color heatmap and a
peak mask focused on the brightest areas in the current frame.
"""

from __future__ import annotations

import argparse
import binascii
import pathlib
import struct
import zlib


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def parse_ppm(path: pathlib.Path):
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise ValueError("input is not a binary PPM file")
    pos = 2

    def token() -> bytes:
        nonlocal pos
        while pos < len(data) and data[pos] in b" \t\r\n":
            pos += 1
        if pos < len(data) and data[pos] == ord("#"):
            while pos < len(data) and data[pos] not in b"\r\n":
                pos += 1
            return token()
        start = pos
        while pos < len(data) and data[pos] not in b" \t\r\n":
            pos += 1
        return data[start:pos]

    width = int(token())
    height = int(token())
    maxval = int(token())
    if maxval != 255:
        raise ValueError("only 8-bit PPM is supported")
    while pos < len(data) and data[pos] in b" \t\r\n":
        pos += 1
    rgb = data[pos:]
    expected = width * height * 3
    if len(rgb) < expected:
        raise ValueError("truncated PPM payload")
    return width, height, rgb[:expected]


def heat_color(t: float) -> tuple[int, int, int]:
    stops = [
        (0.00, (0, 0, 0)),
        (0.20, (0, 0, 180)),
        (0.40, (0, 220, 255)),
        (0.60, (0, 220, 40)),
        (0.78, (255, 230, 0)),
        (0.92, (255, 80, 0)),
        (1.00, (255, 255, 255)),
    ]
    for (a_pos, a), (b_pos, b) in zip(stops, stops[1:]):
        if t <= b_pos:
            f = 0 if b_pos == a_pos else (t - a_pos) / (b_pos - a_pos)
            return tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(3))
    return stops[-1][1]


def write_png(path: pathlib.Path, width: int, height: int, rgb: bytes) -> None:
    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + ctype
            + payload
            + struct.pack(">I", binascii.crc32(ctype + payload) & 0xFFFFFFFF)
        )

    rows = bytearray()
    stride = width * 3
    for y in range(height):
        rows.append(0)
        rows.extend(rgb[y * stride : (y + 1) * stride])
    payload = PNG_SIG
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(bytes(rows), 6))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)


def peakmask_path_for(output: pathlib.Path) -> pathlib.Path:
    stem = output.stem
    if stem.endswith("-heatmap"):
        stem = stem[: -len("-heatmap")]
    return output.with_name(stem + "-peakmask" + output.suffix)


def pq_eotf(x: float | None) -> float | None:
    if x is None:
        return None
    m1 = 2610.0 / 4096 * 1.0 / 4
    m2 = 2523.0 / 4096 * 128
    c1 = 3424.0 / 4096
    c2 = 2413.0 / 4096 * 32
    c3 = 2392.0 / 4096 * 32
    v = x ** (1.0 / m2)
    v = max(v - c1, 0.0) / (c2 - c3 * v)
    return (v ** (1.0 / m1)) * 10000.0


def format_optional_nits(value: float | None) -> str:
    if value is None:
        return "不可用"
    return f"{value:.1f} nits"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--clip-low", type=float, default=0.0)
    parser.add_argument("--clip-high", type=float, default=100.0)
    parser.add_argument("--report", type=pathlib.Path)
    parser.add_argument("--mpv-max-pq-y", type=float)
    parser.add_argument("--mpv-avg-pq-y", type=float)
    args = parser.parse_args()

    width, height, pixels = parse_ppm(args.input)

    lumas = []
    for i in range(0, len(pixels), 3):
        r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
        lumas.append(0.2126 * r + 0.7152 * g + 0.0722 * b)

    sorted_luma = sorted(lumas)
    lo = sorted_luma[int((len(sorted_luma) - 1) * max(args.clip_low, 0) / 100)]
    hi = sorted_luma[int((len(sorted_luma) - 1) * min(args.clip_high, 100) / 100)]
    if hi <= lo:
        hi = max(sorted_luma[-1], lo + 1)

    out = bytearray(width * height * 3)
    for idx, lum in enumerate(lumas):
        t = min(max((lum - lo) / (hi - lo), 0.0), 1.0) ** 0.75
        r, g, b = heat_color(t)
        j = idx * 3
        out[j : j + 3] = bytes((r, g, b))
    write_png(args.output, width, height, bytes(out))

    peak = bytearray(width * height * 3)
    max_luma = max(sorted_luma[-1], 1.0)
    for idx, lum in enumerate(lumas):
        ratio = lum / max_luma
        if ratio >= 0.98:
            color = (255, 255, 255)
        elif ratio >= 0.90:
            color = (255, 0, 0)
        elif ratio >= 0.75:
            color = (255, 128, 0)
        elif ratio >= 0.60:
            color = (255, 230, 0)
        else:
            color = (0, 0, 0)
        j = idx * 3
        peak[j : j + 3] = bytes(color)
    peak_path = peakmask_path_for(args.output)
    write_png(peak_path, width, height, bytes(peak))

    top_01 = sorted_luma[int((len(sorted_luma) - 1) * 0.999)]
    top_1 = sorted_luma[int((len(sorted_luma) - 1) * 0.99)]
    print(f"Wrote: {args.output}")
    print(f"Wrote: {peak_path}")
    print("Peak mask: white=>=98% of this frame max, red=>=90%, orange=>=75%, yellow=>=60%")
    print(f"Luma p99={top_1:.1f}, p99.9={top_01:.1f}, max={sorted_luma[-1]:.1f} (8-bit relative)")

    if args.report:
        avg_luma = sum(lumas) / len(lumas)
        p50 = sorted_luma[int((len(sorted_luma) - 1) * 0.50)]
        p90 = sorted_luma[int((len(sorted_luma) - 1) * 0.90)]
        p99 = sorted_luma[int((len(sorted_luma) - 1) * 0.99)]
        lines = [
            "HDR 亮度热力图报告",
            "",
            "结论先看：",
            "- 这份报告只分析触发热力图时的当前一帧，不代表整部视频平均亮度。",
            "- 热力图来自 mpv 截图后的 8-bit 相对亮度，不能直接当作真实 nits。",
            "- 峰值区域图用于找当前画面里最亮的相对区域：白色最亮，红色次之，橙色/黄色再低。",
            "- 如果 mpv 提供 PQ(Y)，下面的 max-nits/avg-nits 才更接近 HDR 输出亮度参考。",
            "",
            "生成文件：",
            f"- 原始截图 PPM：{args.input}",
            f"- 亮度热力图 PNG：{args.output}",
            f"- 峰值区域图 PNG：{peak_path}",
            f"- 分辨率：{width}x{height}",
            "",
            "峰值区域图颜色说明：",
            "- 白色：>= 当前帧最大相对亮度的 98%",
            "- 红色：>= 当前帧最大相对亮度的 90%",
            "- 橙色：>= 当前帧最大相对亮度的 75%",
            "- 黄色：>= 当前帧最大相对亮度的 60%",
            "- 黑色：低于 60%",
            "",
            "当前帧截图相对亮度（0-255，来自截图管线，只适合同一环境下互相比较）：",
            f"- avg 平均亮度：{avg_luma:.2f}",
            f"- p50 中位亮度：{p50:.2f}",
            f"- p90 90% 像素低于此亮度：{p90:.2f}",
            f"- p99 99% 像素低于此亮度：{p99:.2f}",
            f"- p99.9 99.9% 像素低于此亮度：{top_01:.2f}",
            f"- max 当前帧截图最高相对亮度：{sorted_luma[-1]:.2f}",
            "",
        ]
        if args.mpv_max_pq_y is not None or args.mpv_avg_pq_y is not None:
            max_nits = pq_eotf(args.mpv_max_pq_y) if args.mpv_max_pq_y is not None else None
            avg_nits = pq_eotf(args.mpv_avg_pq_y) if args.mpv_avg_pq_y is not None else None
            lines += [
                "mpv 输出 PQ(Y) 参考值（当前渲染帧/状态）：",
                f"- max-pq-y：{args.mpv_max_pq_y if args.mpv_max_pq_y is not None else '不可用'}",
                f"- max-nits 估算峰值亮度：{format_optional_nits(max_nits)}",
                f"- avg-pq-y：{args.mpv_avg_pq_y if args.mpv_avg_pq_y is not None else '不可用'}",
                f"- avg-nits 估算平均亮度：{format_optional_nits(avg_nits)}",
            ]
        else:
            lines.append("mpv 输出 PQ(Y)：当前截图状态未提供，无法估算 nits。")
        args.report.write_text("\n".join(str(x) for x in lines) + "\n", encoding="utf-8-sig")
        print(f"Wrote: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

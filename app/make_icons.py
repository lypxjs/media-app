"""生成 Android 启动图标（mipmap），复用浏览页那套纯 Python PNG 逻辑。

需生成：
  mipmap-mdpi/ic_launcher.png    48
  mipmap-hdpi/ic_launcher.png    72
  mipmap-xhdpi/ic_launcher.png   96
  mipmap-xxhdpi/ic_launcher.png  144
  mipmap-xxxhdpi/ic_launcher.png 192

launcher 图标应留出安全边距（系统会裁成圆形/方形），
故实际图形占画布约 76%，四周留透明。
"""
import os
import struct
import zlib

RES = r"D:\codebuddy\2026-10-14-21-50-02\android\app\src\main\res"

BG = (15, 17, 21)
FG = (233, 236, 242)


def make_icon(size, scale=0.76):
    """scale = 图形占画布比例（其余留透明边距）。"""
    W = H = size
    pad = (1 - scale) / 2 * W          # 四周留白
    inner = W * scale

    cx = cy = W / 2.0
    bw = inner * 0.34        # 三角底边半宽
    bh = inner * 0.46        # 三角半高
    x0 = cx - bw * 0.85
    y0, y1 = cy - bh / 2, cy + bh / 2
    tx = x0 + bw * 1.70

    A = (x0, y0)
    B = (x0, y1)
    C = (tx, cy)

    def side(P1, P2, Px, Py):
        return (P2[0] - P1[0]) * (Py - P1[1]) - (P2[1] - P1[1]) * (Px - P1[0])

    def in_tri(px, py):
        d1 = side(A, B, px, py)
        d2 = side(B, C, px, py)
        d3 = side(C, A, px, py)
        neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
        pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
        return not (neg and pos)

    r = inner * 0.215
    cxr0 = cx - inner / 2
    cyr0 = cy - inner / 2

    def in_round_rect(px, py):
        qx = min(max(px, cxr0 + r), cxr0 + inner - r)
        qy = min(max(py, cyr0 + r), cyr0 + inner - r)
        return (px - qx) ** 2 + (py - qy) ** 2 <= r * r

    SS = 2
    rows = []
    for y in range(H):
        row = bytearray([0])
        for x in range(W):
            hit_t = hit_o = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = x + (sx + 0.5) / SS
                    py = y + (sy + 0.5) / SS
                    if in_round_rect(px, py):
                        hit_o += 1
                        if in_tri(px, py):
                            hit_t += 1
            total = SS * SS
            if hit_o == 0:
                row += bytes((0, 0, 0, 0))
                continue
            a_t = hit_t / total
            a_o = hit_o / total
            c = tuple(int(FG[i] * a_t + BG[i] * (1 - a_t)) for i in range(3))
            row += bytes((c[0], c[1], c[2], int(round(a_o * 255))))
        rows.append(bytes(row))

    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

if __name__ == "__main__":
    for name, size in DENSITIES.items():
        d = os.path.join(RES, f"mipmap-{name}")
        os.makedirs(d, exist_ok=True)
        png = make_icon(size)
        path = os.path.join(d, "ic_launcher.png")
        with open(path, "wb") as f:
            f.write(png)
        print(f"  mipmap-{name}/ic_launcher.png  {size}x{size}  {len(png)} 字节")
    print("启动图标生成完成")
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从品牌图母版生成页面真正要用的两个小尺寸。

母版 static/bit-api-icon-v3.png 是 1254×1254、946 KB。favicon 实际渲染 16/32 px、
顶栏 brandmark 是 30 px CSS —— 直接把母版挂上去,等于每个访客每次打开都下将近 1 MB
去画一个小方块(/static/ 是 no-cache,浏览器每次都要回源)。

所以母版只当源文件留着,页面引下面这两个降采样产物:
  bit-api-icon-32.png    favicon
  bit-api-icon-180.png   apple-touch-icon,同时给顶栏 brandmark(30 px CSS 到 6× 屏都够)

换母版(v4、改配色)之后跑一遍这个脚本重新生成,别手改产物 ——
deploy.ps1 会在部署时校验图标哈希,产物和母版对不上时那条校验才有意义。

依赖 Pillow,只此一处用到,所以没进 requirements(运行时和 CI 都不需要它):
    python -m pip install Pillow && python scripts/make_icons.py
"""
import os
import sys

MASTER = "static/bit-api-icon-v3.png"
# 调色板 256 色:实测 32 px 完全无损,180 px 最大通道误差 7/255(肉眼看不出),
# 比直存 RGB 小三分之一。母版有 9000 多种颜色的噪点,直存压不动。
OUTPUTS = ((32, "static/bit-api-icon-32.png"),
           (180, "static/bit-api-icon-180.png"))


def main():
    try:
        from PIL import Image
    except ImportError:
        sys.exit("需要 Pillow:python -m pip install Pillow")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = Image.open(os.path.join(root, MASTER)).convert("RGB")
    for size, rel in OUTPUTS:
        path = os.path.join(root, rel)
        im = src.resize((size, size), Image.LANCZOS).quantize(colors=256)
        im.save(path, "PNG", optimize=True)
        print("%-30s %d×%d  %.1f KB"
              % (rel, size, size, os.path.getsize(path) / 1024))


if __name__ == "__main__":
    main()

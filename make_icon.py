# -*- coding: utf-8 -*-
"""icon.png 또는 icon.ico 를 윈도우 아이콘 규격(정사각형·다중 크기)으로 변환해 app.ico 생성"""
from pathlib import Path

from PIL import Image

BASE = Path(__file__).parent
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def newest_source():
    cands = [p for p in (BASE / "icon.png", BASE / "icon.ico") if p.exists()]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def main():
    src = newest_source()
    if src is None:
        print("아이콘 원본 없음 (icon.png 또는 icon.ico)")
        return 1
    im = Image.open(src)
    im.load()
    im = im.convert("RGBA")

    # 여백을 넣어 정사각형으로 (잘리지 않게)
    side = max(im.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))

    out = BASE / "app.ico"
    canvas.save(out, sizes=SIZES)
    print(f"{src.name} ({im.width}x{im.height}) -> app.ico (정사각 {side}px, {len(SIZES)}개 크기)")

    # 화면 헤더에 쓸 아바타 (tkinter 가 PIL 없이 읽을 수 있게 PNG 로)
    canvas.resize((96, 96), Image.LANCZOS).save(BASE / "avatar.png")
    print("avatar.png (96px) 생성")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

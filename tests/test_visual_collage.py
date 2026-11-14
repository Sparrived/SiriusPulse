"""多角色参考拼图的业务验收：四张脸一张图，位置与提示词一一对应。

业务视角：
- 四个角色拼成一张 2×2 参考图，名字按从左到右、从上到下排列；
- 第五个人另起一张拼图，参考图总数可以按上限截断；
- 坏图只跳过那一个角色，后面的位置说明不会错位；
- 透明背景合成到白底上，不会在画面上留出黑块。
"""

from __future__ import annotations

import io

from PIL import Image

from sirius_pulse.visual import collage


def _png(color: tuple[int, int, int] = (90, 80, 160), size=(48, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _rgba_png(color: tuple[int, int, int, int], size: tuple[int, int] = (48, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_collage_packs_four_people_into_one_reference_image():
    people = [
        (name, _png((index * 30, 80, 160))) for index, name in enumerate(("a", "b", "c", "d"))
    ]
    refs = collage.build_reference_collages(people)

    assert len(refs) == 1
    assert refs[0].names == ("a", "b", "c", "d")
    assert (refs[0].columns, refs[0].rows) == (2, 2)
    with Image.open(io.BytesIO(refs[0].data)) as image:
        assert image.size == (
            2 * collage.CELL + 3 * collage.GAP,
            2 * collage.CELL + 3 * collage.GAP,
        )

    note = collage.reference_note(refs)
    assert "左上=a" in note
    assert "右上=b" in note
    assert "左下=c" in note
    assert "右下=d" in note
    assert "不要把拼图网格" in note


def test_collage_splits_five_people_and_caps_reference_images():
    people = [(f"p{index}", _png((index * 20, 100, 180))) for index in range(5)]
    refs = collage.build_reference_collages(people)

    assert len(refs) == 2
    assert refs[0].names == ("p0", "p1", "p2", "p3")
    assert refs[1].names == ("p4",)
    assert len(collage.build_reference_collages(people, max_images=1)) == 1


def test_collage_skips_broken_image_without_shifting_positions():
    people = [
        ("bad", b"not an image"),
        ("b", _png((10, 20, 30))),
        ("c", _png((40, 50, 60))),
        ("d", _png((70, 80, 90))),
    ]
    refs = collage.build_reference_collages(people)

    assert len(refs) == 1
    assert refs[0].names == ("b", "c", "d")
    note = collage.reference_note(refs)
    assert "左上=b" in note
    assert "右上=c" in note
    assert "左下=d" in note
    assert "bad" not in note


def test_collage_composites_transparency_on_white():
    # 全透明像素本身是黑色；合成到白底后格子里应该是亮色，而不是黑块。
    people = [
        ("ghost", _rgba_png((0, 0, 0, 0))),
        ("solid", _png((200, 30, 30))),
    ]
    refs = collage.build_reference_collages(people)

    assert len(refs) == 1
    assert refs[0].names == ("ghost", "solid")
    with Image.open(io.BytesIO(refs[0].data)) as image:
        assert image.mode == "RGB"
        # 第一格中心：透明图缩略后居中贴在这一格里，白底直接透出来。
        pixel = image.getpixel((collage.GAP + collage.CELL // 2, collage.GAP + collage.CELL // 2))
    assert all(channel > 240 for channel in pixel), pixel

"""NapCat 协议解析工具 —— 独立于 Adapter 和 Bridge 的纯转换逻辑。

从原始 OneBot v11 事件中提取结构化信息，不依赖 WebSocket 或引擎。
"""

from __future__ import annotations

from typing import Any


def _face_to_text(data: dict[str, Any]) -> str:
    from sirius_chat.core.prompt_factory import PromptFactory
    face_id = str(data.get("id", ""))
    name = _QQ_FACE_NAMES.get(face_id)
    return PromptFactory.render_face(face_id, name)


_QQ_FACE_NAMES: dict[str, str] = {
    "0": "微笑", "1": "撇嘴", "2": "色", "3": "发呆", "4": "得意",
    "5": "流泪", "6": "害羞", "7": "闭嘴", "8": "睡", "9": "大哭",
    "10": "尴尬", "11": "发怒", "12": "调皮", "13": "呲牙", "14": "惊讶",
    "15": "难过", "16": "酷", "17": "冷汗", "18": "抓狂", "19": "吐",
    "20": "偷笑", "21": "愉快", "22": "白眼", "23": "傲慢", "24": "饥饿",
    "25": "困", "26": "惊恐", "27": "流汗", "28": "憨笑", "29": "悠闲",
    "30": "奋斗", "31": "咒骂", "32": "疑问", "33": "嘘", "34": "晕",
    "35": "折磨", "36": "衰", "37": "骷髅", "38": "敲打", "39": "再见",
    "40": "发抖", "41": "爱情", "42": "跳跳", "43": "猪头", "44": "拥抱",
    "45": "蛋糕", "46": "闪电", "47": "炸弹", "48": "刀", "49": "足球",
    "50": "便便", "51": "咖啡", "52": "饭", "53": "玫瑰", "54": "凋谢",
    "55": "爱心", "56": "心碎", "57": "礼物", "58": "太阳", "59": "月亮",
    "60": "赞", "61": "踩", "62": "握手", "63": "胜利", "64": "飞吻",
    "65": "怄火", "66": "西瓜", "67": "冷酷", "68": "擦汗", "69": "抠鼻",
    "70": "鼓掌", "71": "糗大了", "72": "坏笑", "73": "左哼哼", "74": "右哼哼",
    "75": "哈欠", "76": "鄙视", "77": "委屈", "78": "快哭了", "79": "阴险",
    "80": "亲亲", "81": "吓", "82": "可怜", "83": "菜刀", "84": "啤酒",
    "85": "篮球", "86": "乒乓", "87": "示爱", "88": "瓢虫", "89": "抱拳",
    "90": "勾引", "91": "拳头", "92": "差劲", "93": "爱你", "94": "NO",
    "95": "OK", "96": "转圈", "97": "磕头", "98": "回头", "99": "跳绳",
    "100": "挥手", "101": "激动", "102": "街舞", "103": "献吻", "104": "左太极",
    "105": "右太极", "106": "闭嘴", "107": "招财猫", "108": "双喜", "109": "鞭炮",
    "110": "灯笼", "111": "K歌", "112": "喝彩", "113": "祈祷", "114": "爆筋",
    "115": "棒棒糖", "116": "奶瓶", "117": "面条", "118": "香蕉", "119": "飞机",
    "120": "开车", "121": "高铁左", "122": "车厢", "123": "高铁右", "124": "多云",
    "125": "下雨", "126": "钞票", "127": "熊猫", "128": "灯泡", "129": "风车",
    "130": "闹钟", "131": "打伞", "132": "彩球", "133": "戒指", "134": "沙发",
    "135": "纸巾", "136": "手枪", "137": "青蛙", "138": "放大镜", "139": "聚光灯",
    "140": "墨镜", "141": "礼物", "142": "烟花", "143": "拜托", "144": "飞鸟",
    "145": "月亮", "146": "星星", "147": "小太阳", "148": "钞票", "149": "彩带",
    "150": "气球", "151": "钻石", "152": "干杯", "153": "音乐", "154": "绿丝带",
    "155": "面条", "156": "蜡烛", "157": "蛋糕", "158": "礼物", "159": "小熊",
    "160": "奶牛", "161": "小鸡", "162": "小狗", "163": "小鱼", "164": "小猫",
    "165": "老鼠", "166": "兔子", "167": "螃蟹", "168": "蝴蝶", "169": "海豚",
    "170": "企鹅", "171": "松鼠", "172": "鲜花", "173": "椰树", "174": "仙人掌",
    "175": "枫叶", "176": "枯叶", "177": "四叶草", "178": "枫叶", "179": "幸运",
    "180": "对勾", "181": "叉", "182": "圈", "183": "三角", "184": "爱心",
    "185": "心碎", "186": "全部", "187": "礼物",
}


def extract_text_from_segments(message: list[dict[str, Any]]) -> str:
    """从 OneBot 消息段数组中提取纯文本。"""
    parts: list[str] = []
    for seg in message:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
        elif seg.get("type") == "face":
            parts.append(_face_to_text(seg.get("data", {})))
    return "".join(parts).strip()


def extract_image_urls(message: list[dict[str, Any]]) -> list[str]:
    """从消息段中提取所有图片 URL。"""
    urls: list[str] = []
    for seg in message:
        if seg.get("type") == "image":
            data = seg.get("data", {})
            url = data.get("url", "") or data.get("file", "")
            if url:
                urls.append(url)
    return urls


def extract_sender_names(event: dict[str, Any]) -> tuple[str, str]:
    sender = event.get("sender", {})
    nickname = str(sender.get("nickname", "") or "").strip()
    card = str(sender.get("card", "") or "").strip()
    return nickname, card


def sanitize_image_name(name: str) -> str:
    from urllib.parse import unquote
    text = unquote(str(name or "").strip().strip("'\"")).replace("\r", " ").replace("\n", " ")
    text = text.replace("[", "(").replace("]", ")")
    return text[:80].strip()


def extract_image_name(seg: dict[str, Any], index: int, fallback_prefix: str = "未命名图片") -> str:
    data = seg.get("data", {})
    candidates = [
        data.get("filename", ""),
        data.get("file_name", ""),
        data.get("name", ""),
        data.get("file", ""),
        data.get("url", ""),
    ]
    for raw in candidates:
        text = str(raw or "").strip()
        if text:
            from urllib.parse import urlparse
            parsed = urlparse(text)
            for candidate in (parsed.path, text):
                normalized = str(candidate or "").strip().replace("\\", "/").rstrip("/")
                if not normalized or normalized.startswith(("data:", "base64:")):
                    continue
                name = sanitize_image_name(normalized.split("/")[-1])
                if name:
                    return name
    return f"{fallback_prefix}_{index}"


def dedupe_image_name(name: str, counter: dict[str, int]) -> str:
    seen = counter.get(name, 0) + 1
    counter[name] = seen
    if seen == 1:
        return name
    stem, dot, suffix = name.rpartition(".")
    if dot:
        return f"{stem}#{seen}.{suffix}"
    return f"{name}#{seen}"


def build_image_label(seg: dict[str, Any], index: int, label_prefix: str, counter: dict[str, int]) -> str:
    from sirius_chat.core.prompt_factory import PromptFactory
    image_name = extract_image_name(seg, index)
    display_name = dedupe_image_name(image_name, counter)
    return PromptFactory.render_image(display_name, label_prefix)

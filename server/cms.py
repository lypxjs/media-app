"""
源站解析：把苹果 CMS 的原始 JSON 转成索引库的记录。

两个源站的字段基本一致（都是 MacCMSV10 风格），差异在于：
- 剧集串格式：`名称$url#名称$url`（vod_play_url）
- 无水印影视部分条目vod_douban_score 才有值
"""

import re

# vod_play_url 形如 "全集$https://x/index.m3u8#第02集$https://y/2.m3u8"
_EP_SEP = "#"
_NAME_URL_SEP = "$"


def parse_episodes(play_url: str | None) -> list[str]:
    """解析剧集列表，只返回 URL 数组（LibreTV 的 episodes 结构）。"""
    if not play_url:
        return []
    out = []
    for seg in str(play_url).split(_EP_SEP):
        seg = seg.strip()
        if not seg:
            continue
        # 取最后一个 $ 之后的部分（片名本身可能含 $）
        idx = seg.rfind(_NAME_URL_SEP)
        url = (seg[idx + 1:] if idx >= 0 else seg).strip()
        if url and url.lower().startswith(("http://", "https://")):
            out.append(url)
    return out


def _num(v, cast=int, default=0):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def to_record(src: str, v: dict) -> dict | None:
    """把一条 CMS list 元素转成 videos 表记录。"""
    vid = v.get("vod_id")
    name = (v.get("vod_name") or "").strip()
    if vid is None or not name:
        return None
    return {
        "src": src,
        "vod_id": int(vid),
        "name": name,
        "pic": v.get("vod_pic") or "",
        "type_id": _num(v.get("type_id"), int, None) if v.get("type_id") is not None else None,
        "type_name": v.get("type_name") or "",
        "year": str(v.get("vod_year") or "").strip(),
        "area": (v.get("vod_area") or "").strip(),
        "remarks": (v.get("vod_remarks") or "").strip(),
        "score": _num(v.get("vod_score") or v.get("vod_douban_score"), float, 0.0),
        "hits": _num(v.get("vod_hits"), int, 0),
        "serial": str(v.get("vod_serial") or "").strip(),
        "total": str(v.get("vod_total") or "").strip(),
        "actor": _trim(v.get("vod_actor")),
        "director": _trim(v.get("vod_director")),
        "plot": _trim(v.get("vod_plot") or v.get("vod_content")),
        "play_from": v.get("vod_play_from") or "",
        "episodes": parse_episodes(v.get("vod_play_url")),
        "vod_time": v.get("vod_time") or v.get("vod_time_add") or "",
    }


def _trim(s, limit=400) -> str:
    """压缩长文本：多行转空格、限长，避免详情接口的大字段撑爆索引。"""
    if not s:
        return ""
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:limit]


def merge_list_item(src: str, v: dict) -> dict:
    """列表页元素 → 记录（列表不含剧集，episodes 为空）。

    后续详情抓取会补齐 episodes；此处先入库保证翻页可用，
    因为列表数据已有封面/分类/集数，正是「翻找」所需的全部字段。
    """
    r = to_record(src, v)
    if r is None:
        return {}
    r["episodes"] = []
    return r

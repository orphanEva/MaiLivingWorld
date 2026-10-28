"""B站公开热门视频元信息（仅标题、简介、链接，不声称实际看过视频）。"""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit
from .explore import Story, compact, _public_dns_only, _NoRedirect, MAX_BYTES
import urllib.request

POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1"


def parse_popular(content: bytes, limit: int = 20) -> list[Story]:
    if len(content) > MAX_BYTES:
        raise ValueError("bilibili response too large")
    obj=json.loads(content)
    if not isinstance(obj,dict) or obj.get('code') != 0:
        raise ValueError("B站 API 业务失败/风控（不进行绕过）")
    results=obj.get('data') or {}
    items=results.get('list') or []
    if not isinstance(items,list):
        raise ValueError("unexpected B站 JSON")
    ret=[]
    for video in items[:limit]:
        if not isinstance(video,dict):
            continue
        bvid=str(video.get('bvid',''))
        if not re.fullmatch(r'BV[0-9A-Za-z]{10}',bvid):
            continue
        title=compact(video.get('title',''),180)
        if not title:
            continue
        summary=compact(video.get('desc',''),350)
        pub=video.get('pubdate')
        published=float(pub) if isinstance(pub,(float,int)) and pub>0 else None
        link=f"https://www.bilibili.com/video/{bvid}"
        ret.append(Story(hashlib.sha256(link.encode()).hexdigest()[:24],"B站热门",title,link,summary,published))
    return ret


def fetch_popular(timeout: int=12) -> list[Story]:
    # 不使用带登录凭据的 API，不发表评论，不绕过访问控制或风控。
    _public_dns_only('api.bilibili.com')
    request=urllib.request.Request(POPULAR_URL,headers={
        "User-Agent":"Mozilla/5.0 (compatible; MaiLivingWorld/0.1; read-only)",
        "Accept":"application/json",
        "Referer":"https://www.bilibili.com/",
    })
    opener=urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=max(3,min(timeout,30))) as response:
        raw=response.read(MAX_BYTES+1)
    return parse_popular(raw)

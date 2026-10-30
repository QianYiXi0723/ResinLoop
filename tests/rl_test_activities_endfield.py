# -*- coding: utf-8 -*-
"""终末地活动源（fz.wiki 结构化日程）离线验证 + 管线接线检查。

背景（2026-10-31）：
    终末地官方没有活动日历接口（公告不带截止时间），实装采用社区方案：
    fz.wiki「活动」文章的 endfieldCardActivityIndex 节点——编辑者维护的
    结构化 timeRanges/calendarRange（open/close 直出，零启发式）。
    常驻类（无截止时间）没有"结束"可提醒，抓取时即过滤。

覆盖：
  C1  节点挖掘：current（attrs.data）与 legacy（attrs 直挂）双形态都认
  C2  fetch 级（monkeypatch 会话，零网络）：进行中保留/已结束过滤/
      常驻过滤/无名跳过/输出字段契约（end 可被 _parse_time 解析）
  C3  按结束时间升序排序
  C4  FETCHERS/SOURCES 已注册 endfield；UNSUPPORTED 不再含终末地
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import activities as A  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok  : " if ok else "FAIL: ") + name + (("  | " + str(detail)) if detail else ""))


def wiki_time(dt):
    return dt.strftime("%Y/%m/%d %H:%M:%S")


NOW = datetime.now()
ITEM_TYPE = "endfieldCardActivityIndex__activities"


def item_node(name, start, end, *, legacy=False, activity_id=""):
    """按上游真实形态构造一个活动子块。"""
    ranges = [{"open": wiki_time(start), "close": wiki_time(end) if end else ""}]
    data = {"name": name, "activityId": activity_id or f"id_{name}",
            "timeRanges": ranges, "tags": ["限时活动"],
            "calendarRange": dict(ranges[0]), "linkTitle": f"活动/{name}",
            "tabImgUrl": ""}
    if legacy:
        return {"type": ITEM_TYPE, "attrs": dict(data)}
    return {"type": ITEM_TYPE,
            "attrs": {"itemType": ITEM_TYPE, "data": data}}


FIXTURE = {
    "revision": {"contentJson": {"content": [
        {"type": "paragraph", "attrs": {"id": "intro"}},
        {"type": "endfieldCardActivityIndex",
         "attrs": {"versions": [{"open": wiki_time(NOW - timedelta(days=30)),
                                  "close": wiki_time(NOW + timedelta(days=30))}],
                   "id": "idx", "generatedBy": "test"},
         "content": [
             item_node("予众魂的新光影", NOW - timedelta(days=1),
                       NOW + timedelta(days=7), activity_id="web_activity_ghost"),
             item_node("百趣演武", NOW - timedelta(days=2),
                       NOW + timedelta(days=14), legacy=True,
                       activity_id="activity_gambling"),
             item_node("星河共我签到", NOW + timedelta(days=3),
                       NOW + timedelta(days=10), activity_id="activity_checkin"),
             item_node("常驻样例", NOW - timedelta(days=20), None,
                       activity_id="perm"),
             item_node("已结束样例", NOW - timedelta(days=10),
                       NOW - timedelta(days=5), activity_id="ended"),
             {"type": ITEM_TYPE, "attrs": {"itemType": ITEM_TYPE,
                                           "data": {"timeRanges": []}}},
         ]},
    ]}},
}


class FakeResponse:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, payload):
        self._p = payload

    def get(self, url, **kwargs):
        return FakeResponse(self._p)


# ===================== C1 节点挖掘（双形态） =====================
print("== C1 节点挖掘：current+legacy 双形态 ==")
nodes = A._endfield_activity_nodes(FIXTURE)
check("C1a 挖到 5 个有名节点（无名跳过）", len(nodes) == 5, len(nodes))
check("C1b current 形态解包 attrs.data",
      any(n.get("name") == "予众魂的新光影" for n in nodes))
check("C1c legacy 形态（attrs 直挂）也认",
      any(n.get("name") == "百趣演武" for n in nodes))

# ===================== C2 fetch 级 =====================
print("== C2 fetch（monkeypatch 会话，零网络） ==")
orig_session = A._new_session
A._new_session = lambda: FakeSession(FIXTURE)
try:
    items = A.fetch_endfield_activities()
finally:
    A._new_session = orig_session

check("C2a 只留 3 条限时（常驻/已结束/无名全过滤）", len(items) == 3,
      [it["title"] for it in items])
titles = [it["title"] for it in items]
check("C2b 按结束时间升序（排序键=end，非插入序）",
      titles == ["予众魂的新光影", "星河共我签到", "百趣演武"], titles)
first = items[0]
check("C2c 字段契约：game/kind/remote_id",
      first["game"] == "endfield" and first["game_name"] == "终末地"
      and first["kind"] == "act" and first["remote_id"] == "web_activity_ghost",
      first)
check("C2d end 可被管线解析（_parse_time 不返 None）",
      A._parse_time(first["end"]) is not None, first["end"])
check("C2e reward_ok=True（不被资源过滤器丢掉）", first.get("reward_ok") is True)

# ===================== C3 注册与 UNSUPPORTED =====================
print("== C3 管线接线 ==")
check("C3a FETCHERS 已注册 endfield",
      A.FETCHERS.get("endfield") is A.fetch_endfield_activities)
check("C3b SOURCES 有终末地来源说明", "endfield" in A.SOURCES)
check("C3c UNSUPPORTED 不再含终末地",
      all(item.get("game") != "endfield" for item in A.UNSUPPORTED),
      A.UNSUPPORTED)

passed = sum(1 for _, ok in results if ok)
print("\n[结论] %d/%d 通过%s" % (passed, len(results),
      "" if passed == len(results) else "  ← 存在 FAIL"))
sys.exit(0 if passed == len(results) else 1)

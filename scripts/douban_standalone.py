"""Pinned Douban sync with throttling and a non-destructive legacy book adapter."""
import os
import sys
import time
import requests
from urllib.parse import urlsplit
from notion_client import Client
from notion_client.errors import APIResponseError

raw_get = requests.get
def checked_get(*args, **kwargs):
    kwargs.setdefault("timeout", 30)
    response = raw_get(*args, **kwargs)
    if response.status_code in (400, 401) and urlsplit(args[0] if args else kwargs.get("url", "")).hostname == "frodo.douban.com":
        headers = dict(kwargs.get("headers") or {})
        if headers.get("authorization"):
            headers.pop("authorization", None)
            public_kwargs = dict(kwargs, headers=headers)
            public_response = raw_get(*args, **public_kwargs)
            if public_response.ok:
                response = public_response
    if not response.ok:
        try:
            data = response.json()
            code = data.get("code", data.get("error_code", "unknown"))
            message = str(data.get("msg", data.get("message", "")))[:120]
        except ValueError:
            code, message = "unknown", ""
        raise RuntimeError(f"豆瓣接口 HTTP {response.status_code}; code={code}; {message}")
    return response
requests.get = checked_get

original_request = Client.request
last = [0.0]
def paced(self, *args, **kwargs):
    body = kwargs.get("body") or {}
    if str(kwargs.get("method", "")).upper() == "DELETE" or body.get("archived") or body.get("in_trash"):
        raise RuntimeError("已阻止删除或归档操作")
    for attempt in range(10):
        time.sleep(max(0, 0.6 - (time.monotonic() - last[0])))
        last[0] = time.monotonic()
        try:
            return original_request(self, *args, **kwargs)
        except APIResponseError as error:
            if error.status != 429:
                raise
            time.sleep(max(2, float(error.headers.get("Retry-After", 2))))
    raise RuntimeError("Notion 持续限流；已有记录保留")
Client.request = paced

from douban2notion import douban, notion_helper, utils
# Upstream book code prints complete Notion records; keep those out of public logs.
douban.print = lambda *args, **kwargs: None

if len(sys.argv) > 1 and sys.argv[1] == "book" and os.getenv("LEGACY_BOOK_TEMPLATE") == "true":
    Helper = notion_helper.NotionHelper
    original_init = Helper.__init__
    original_query_all = Helper.query_all
    original_properties = utils.get_properties
    active = [None]

    def init(self, kind):
        original_init(self, kind)
        if (self.book_database_id or "").replace("-", "") != "bcc0193e8d6883e1a59d810a897c2451":
            raise RuntimeError("图书数据库校验失败，已停止写入")
        self.publisher_database_id = self.database_id_dict.get("出版社")
        if not self.publisher_database_id:
            raise RuntimeError("缺少现有出版社关联数据库")
        active[0] = self

    def query_all(self, database_id, *args, **kwargs):
        rows = original_query_all(self, database_id, *args, **kwargs)
        if database_id == self.book_database_id:
            for row in rows:
                properties = row.get("properties", {})
                if "豆瓣" in properties and "豆瓣链接" not in properties:
                    properties["豆瓣链接"] = properties["豆瓣"]
        return rows

    def properties(data, types):
        result = original_properties(data, types)
        if "豆瓣链接" in result:
            result["豆瓣"] = result.pop("豆瓣链接")
        # The existing template uses page covers, not a file property.
        result.pop("封面", None)
        publishers = result.pop("出版社", None)
        if publishers is not None:
            names = [entry["name"] for entry in publishers.get("multi_select", [])]
            helper = active[0]
            result["出版社"] = {"relation": [
                {"id": helper.get_relation_id(name, helper.publisher_database_id, "https://www.notion.so/icons/groups_gray.svg")}
                for name in names
            ]}
        return result

    Helper.__init__ = init
    Helper.query_all = query_all
    utils.get_properties = properties

if __name__ == "__main__":
    douban.main()
    print("数据同步完成；未执行删除或归档。", flush=True)

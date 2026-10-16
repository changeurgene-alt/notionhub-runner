"""Generate aggregate SVG heatmaps from Notion formula values without external services."""
import calendar
import datetime as dt
import os
import sys
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape
import douban_standalone  # installs pacing and safe request handling
from notion_client import Client

def numeric(value):
    if value is None: return 0
    if isinstance(value, (int, float)): return int(value)
    if isinstance(value, str): return int(float(value)) if value.strip() else 0
    if isinstance(value, list): return sum(numeric(x) for x in value)
    if isinstance(value, dict):
        kind=value.get("type")
        if kind and kind in value: return numeric(value[kind])
        for key in ("number", "formula", "rollup", "array"):
            if key in value: return numeric(value[key])
        raise ValueError("无法识别统计数值格式")
    raise ValueError("无法识别统计数值类型")

def svg(counts, year, label, unit):
    jan=dt.date(year,1,1); dec=dt.date(year,12,31)
    start=jan-dt.timedelta(days=jan.weekday())
    weeks=((dec-start).days//7)+1
    width=58+weeks*13; height=163
    total=sum(counts.values())
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="#ffffff"/>',
           f'<text x="18" y="22" font-family="Arial,sans-serif" font-size="14" fill="#24292f">{year} {escape(label)} · {total} {unit}</text>']
    for m in range(1,13):
        day=dt.date(year,m,1); x=37+((day-start).days//7)*13
        parts.append(f'<text x="{x}" y="42" font-family="Arial,sans-serif" font-size="10" fill="#57606a">{m}月</text>')
    palette=["#ebedf0","#9be9a8","#40c463","#30a14e","#216e39"]
    for offset in range((dec-start).days+1):
        day=start+dt.timedelta(days=offset)
        if day.year!=year: continue
        value=counts.get(day.isoformat(),0)
        level=min(4,value)
        x=37+(offset//7)*13; y=49+day.weekday()*13
        parts.append(f'<rect x="{x}" y="{y}" width="10" height="10" rx="2" fill="{palette[level]}"><title>{day.isoformat()}: {value} {unit}</title></rect>')
    for weekday,text in ((0,"一"),(2,"三"),(4,"五")):
        parts.append(f'<text x="18" y="{57+weekday*13}" font-family="Arial,sans-serif" font-size="9" fill="#57606a">{text}</text>')
    parts.append('</svg>')
    return "".join(parts)

def find_chart(client, parent):
    cursor=None
    while True:
        response=client.blocks.children.list(block_id=parent,start_cursor=cursor,page_size=100)
        for block in response["results"]:
            kind=block["type"]; url=""
            if kind=="embed": url=block["embed"].get("url","")
            if kind=="image":
                image=block["image"]
                url=image.get("external",{}).get("url","")
            if any(marker in url for marker in ("heatmap","wereadassets",".notionhub-artifacts/douban/")):
                return block["id"],kind
            if block.get("has_children") and kind not in ("child_database","child_page"):
                found=find_chart(client,block["id"])
                if found: return found
        if not response.get("has_more"): return None
        cursor=response["next_cursor"]

def main():
    stream=sys.argv[1]
    if stream not in ("movie","book"): raise ValueError("未知同步类型")
    client=Client(auth=os.environ["NOTION_TOKEN"])
    database_id=os.environ["DATABASE_ID"]
    root=os.environ["NOTION_CHART_ROOT"]
    year=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).year
    prop="看过" if stream=="movie" else "读过"
    counts={}; cursor=None
    while True:
        response=client.databases.query(database_id=database_id,start_cursor=cursor,page_size=100)
        for row in response["results"]:
            properties=row["properties"]
            date=(properties.get("日期",{}).get("date") or {}).get("start")
            if not date or not date.startswith(str(year)): continue
            date=date[:10]
            counts[date]=counts.get(date,0)+numeric(properties.get(prop))
        if not response.get("has_more"): break
        cursor=response["next_cursor"]
    label,unit=("观影记录","部") if stream=="movie" else ("阅读记录","本")
    relative=f"{stream}/{year}.svg"
    path=Path(os.environ["NOTIONHUB_ARTIFACT_ROOT"])/relative
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(svg(counts,year,label,unit),encoding="utf-8")
    artifact=os.environ["NOTIONHUB_ARTIFACT_PATH"]+"/"+relative
    url=f'https://raw.githubusercontent.com/{os.environ["REPOSITORY"]}/{os.environ["ARTIFACT_BRANCH"]}/{artifact}?v={os.environ["GITHUB_RUN_ID"]}'
    found=find_chart(client,root)
    if not found: raise RuntimeError("未找到现有热力图位置，已保存 SVG，未改动其他区块")
    block_id,kind=found
    if kind=="image": client.blocks.update(block_id=block_id,image={"external":{"url":url}})
    else: client.blocks.update(block_id=block_id,embed={"url":url})
    print(f"{label}热力图完成：{year} 年共 {sum(counts.values())} {unit}",flush=True)

if __name__=="__main__": main()

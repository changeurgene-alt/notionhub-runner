"""Public Douban music collections -> existing Notion music source, no deletes.
MIT License. Copyright (c) 2026 changeurgene-alt.
"""
import argparse, json, os, re, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup

USER = '181905505'
SOURCE = 'ae90193e-8d68-8352-b35c-07af1efd9755'
ARTISTS = '7170193e-8d68-83c9-b2b0-87eba263fe81'
GENRES = 'ee40193e-8d68-82ec-a728-079ab78efabf'
STATES = {'wish':'想听','do':'在听','collect':'听过'}
WEB = requests.Session()
WEB.headers['User-Agent'] = 'Mozilla/5.0 (compatible; PersonalCollectionSync/1.0)'

def parse_collection(html, status):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('h1')
    count = re.search(r'音乐\s*[（(](\d+)[）)]', heading.get_text(' ',strip=True) if heading else '')
    if not count or soup.select_one('.grid-view') is None and int(count[1]) != 0:
        raise RuntimeError('收藏页结构异常或需要验证，保留已有记录')
    rows=[]
    for item in soup.select('.grid-view .item'):
        link=item.select_one('.title a[href]'); img=item.select_one('.pic img'); date=item.select_one('.date')
        if not link or not re.fullmatch(r'https://music.douban.com/subject/\d+/',link['href']):
            raise RuntimeError('无效音乐条目')
        rating=item.select_one('[class*="rating"][class*="-t"]')
        match=re.search(r'rating([1-5])-t', ' '.join(rating.get('class',[]))) if rating else None
        info=item.select_one('.intro')
        parts=[p.strip() for p in info.get_text(' ',strip=True).split('/')] if info else []
        comment=[]
        for li in item.select('.info li'):
            if li.get('class') or li.select_one('.date') or li.select_one('[class*="rating"]'): continue
            text=li.get_text(' ',strip=True)
            if text: comment.append(text)
        rows.append(dict(title=link.get_text(' ',strip=True),url=link['href'],status=status,
            cover=img.get('src') if img else None,date=date.get_text(strip=True) if date else None,
            rating=int(match[1]) if match else None,comment='\n'.join(comment),
            artist=parts[0] if parts else None,genre=parts[-1] if len(parts)>=4 else None))
    return int(count[1]),rows

def fetch_all():
    rows={}; counts={}
    for route,status in STATES.items():
        offset=0; seen=set(); expected=None
        while True:
            response=WEB.get(f'https://music.douban.com/people/{USER}/{route}',params={'start':offset,'sort':'time','mode':'grid'},timeout=30)
            response.raise_for_status(); total,batch=parse_collection(response.text,status)
            if expected is None: expected=total
            if total!=expected: raise RuntimeError('抓取期间收藏数量改变，请重试')
            fresh=[r for r in batch if r['url'] not in seen]
            if not fresh and len(seen)<total: raise RuntimeError('分页未完成，停止写入')
            for row in fresh: seen.add(row['url']);rows[row['url']]=row
            if len(seen)>=total: break
            offset+=15; time.sleep(2)
        if len(seen)!=total: raise RuntimeError('收藏数量校验失败')
        counts[status]=total; time.sleep(2)
    return counts,list(rows.values())

class Notion:
    def __init__(self,token):
        self.session=requests.Session();self.session.headers.update({'Authorization':f'Bearer {token}','Notion-Version':'2025-09-03','Content-Type':'application/json'})
    def call(self,method,path,body=None):
        for attempt in range(6):
            time.sleep(.4)
            r=self.session.request(method,'https://api.notion.com/v1/'+path,json=body,timeout=40)
            if r.status_code==429 or r.status_code>=500:
                time.sleep(max(2,float(r.headers.get('Retry-After',2)))+attempt);continue
            if not r.ok: raise RuntimeError(f'Notion HTTP {r.status_code} ({path.split("/")[0]})')
            return r.json()
        raise RuntimeError('Notion 请求重试耗尽')
    def query(self,source):
        result=[];body={'page_size':100}
        while True:
            d=self.call('POST',f'data_sources/{source}/query',body);result.extend(d['results'])
            if not d['has_more']:return result
            body['start_cursor']=d['next_cursor']
    def relation(self,source,name):
        if source not in (ARTISTS,GENRES):raise RuntimeError('关联目标不允许')
        if source not in self.cache:
            schema=self.call('GET',f'data_sources/{source}')
            title=next(k for k,v in schema['properties'].items() if v['type']=='title')
            self.cache[source]=(title,{''.join(t.get('plain_text',t.get('text',{}).get('content','')) for t in r['properties'][title]['title']):r['id'] for r in self.query(source)})
        title,values=self.cache[source]
        if name not in values:
            p=self.call('POST','pages',{'parent':{'type':'data_source_id','data_source_id':source},'properties':{title:{'title':rich(name)}}});values[name]=p['id']
        return [{'id':values[name]}]

def rich(text):return [{'type':'text','text':{'content':str(text)[i:i+1900]}} for i in range(0,len(str(text)),1900)]
def properties(row,client):
    p={'音乐名':{'title':rich(row['title'])},'豆瓣':{'url':row['url']},'状态':{'status':{'name':row['status']}},'短评':{'rich_text':rich(row['comment'])},'评分':{'select':{'name':'⭐️'*row['rating']} if row['rating'] else None}}
    if row['date'] and re.fullmatch(r'\d{4}-\d{2}-\d{2}',row['date']):p['日期']={'date':{'start':row['date']}}
    if row['artist']:p['歌手']={'relation':client.relation(ARTISTS,row['artist'])}
    if row['genre']:p['分类']={'relation':client.relation(GENRES,row['genre'])}
    return p

def main():
    global SOURCE, ARTISTS, GENRES
    parser=argparse.ArgumentParser();parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
    counts,rows=fetch_all()
    print(json.dumps({'source_counts':counts,'total':len(rows)},ensure_ascii=False))
    if args.dry_run:
        Path(os.getenv('MUSIC_PREVIEW','music-preview.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2));return
    config=json.loads(os.getenv('FREE_CONFIG_JSON','{}'))
    tokens=[config.get('notion',{}).get('targets',{}).get('movie',{}).get('token'),os.getenv('NOTION_TOKEN')]
    client=None
    for token in dict.fromkeys(t for t in tokens if t):
        candidate=Notion(token)
        try:
            db=candidate.call('GET','databases/b830193e-8d68-8289-9d7f-0129e8c7b01a')
            sources=db.get('data_sources',[])
            if len(sources)!=1:raise RuntimeError('音乐源数量异常')
            SOURCE=sources[0]['id']
            schema=candidate.call('GET',f'data_sources/{SOURCE}')['properties']
            if '音乐名' not in schema or '豆瓣' not in schema:raise RuntimeError('音乐库结构异常')
            ARTISTS=schema['歌手']['relation']['data_source_id'];GENRES=schema['分类']['relation']['data_source_id']
            client=candidate;break
        except RuntimeError as error:
            print('连接检查:',str(error));continue
    if client is None:raise RuntimeError('音乐页面尚未授权给现有同步连接，需要仅授权该音乐页面')
    client.cache={};existing={}
    for record in client.query(SOURCE):
        url=record['properties'].get('豆瓣',{}).get('url')
        if url in existing:raise RuntimeError('发现重复豆瓣链接，停止以避免误更新')
        if url:existing[url]=record
    created=updated=unchanged=0
    for row in rows:
        props=properties(row,client);old=existing.get(row['url'])
        body={'properties':props}
        if row['cover'] and (not old or not old.get('cover')):body['cover']={'type':'external','external':{'url':row['cover']}}
        if old:
            # Only source-owned properties; custom notes, page body, and other relations remain intact.
            same=True
            for name,value in props.items():
                kind=next(iter(value));before=old['properties'].get(name,{}).get(kind)
                after=value[kind]
                if kind in ('title','rich_text'):
                    before=''.join(t.get('plain_text','') for t in before or []);after=''.join(t['text']['content'] for t in after)
                elif kind in ('select','status'):
                    before=before.get('name') if before else None;after=after.get('name') if after else None
                elif kind=='relation':before=sorted(t['id'] for t in before or []);after=sorted(t['id'] for t in after)
                elif kind=='date':before=before.get('start') if before else None;after=after.get('start') if after else None
                if before!=after:same=False
            if same and 'cover' not in body:unchanged+=1;continue
            client.call('PATCH',f'pages/{old["id"]}',body);updated+=1
        else:
            body['parent']={'type':'data_source_id','data_source_id':SOURCE}
            client.call('POST','pages',body);created+=1
    actual=client.query(SOURCE)
    actual_urls={r['properties'].get('豆瓣',{}).get('url') for r in actual}
    if not {r['url'] for r in rows}<=actual_urls:raise RuntimeError('同步后核对失败')
    summary={'created':created,'updated':updated,'unchanged':unchanged,'source_counts':counts,'verified':True}
    print(json.dumps(summary,ensure_ascii=False))
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('## 豆瓣音乐同步\n\n'+json.dumps(summary,ensure_ascii=False)+'\n\n仅新增与更新；不会删除原记录。\n')
if __name__=='__main__':main()

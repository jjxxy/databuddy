"""Local-only mock financial portal. All figures are fictional fixtures."""
import csv
import html
import json
import math
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
FILES = {'orders': 'orders.csv', 'refunds': 'refunds.csv', 'ledger': 'ledger.csv'}
RATE_LOCK = threading.Lock()
RATE_SEEN = set()

def rows(name):
    with (DATA / FILES[name]).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def page_number(query, name, default, max_value):
    raw = query.get(name, [str(default)])
    if len(raw) != 1 or not raw[0].isascii() or not raw[0].isdigit():
        raise ValueError('分页参数必须为正整数')
    value = int(raw[0])
    if not 1 <= value <= max_value:
        raise ValueError('分页参数超出范围')
    return value

def document(title, body):
    return ('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+html.escape(title)+'''</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:0;color:#202d39;background:#f4f6f8}header{background:#173a56;color:white;padding:20px 7%}main{max-width:1150px;margin:30px auto;padding:0 20px}a{color:#075d94}table{width:100%;background:white;border-collapse:collapse}th,td{padding:8px;border:1px solid #d8dee5;text-align:left}th{background:#ecf2f6}nav a{display:inline-block;margin:8px 18px 8px 0}.note{color:#516273}</style></head><body><header><strong>华澜贸易　经营数据门户</strong><div>虚构测试系统 · 数据截至 2026 年 10 月</div></header><main>'''+body+'</main></body></html>').encode('utf-8')

class Handler(BaseHTTPRequestHandler):
    server_version='FixturePortal/1.0'
    def log_message(self, fmt, *args):
        print('%s %s' % (self.address_string(), fmt % args), flush=True)
    def reply(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers()
        if self.command != 'HEAD':self.wfile.write(body)
    def error_json(self,status,message,extra=None):
        self.reply(status,json.dumps({'error':message},ensure_ascii=False).encode('utf8'),'application/json; charset=utf-8',extra)
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        uri=urlparse(self.path);q=parse_qs(uri.query,keep_blank_values=True)
        try:
            if uri.path == '/':
                body='''<h1>业务数据查询</h1><p class="note">本系统仅提供合成数据。请按用途选择页面、接口或附件。</p><nav><a href="/orders?page=1">订单查询</a><a href="/downloads/orders.csv">下载订单明细</a><a href="/downloads/refunds.csv">下载退款明细</a><a href="/downloads/ledger.csv">下载财务总账</a><a href="/api/refunds?page=1&amp;page_size=10">退款数据接口</a></nav><p>订单页面按页展示，退款接口支持分页。财务总账来自独立系统，请自行核对期间、单位及数据口径。</p>'''
                return self.reply(200,document('经营数据门户',body),'text/html; charset=utf-8')
            if uri.path == '/orders':
                page=page_number(q,'page',1,10000); records=rows('orders'); size=100; pages=math.ceil(len(records)/size)
                cols=list(records[0]); selected=records[(page-1)*size:page*size]
                table='<table><thead><tr>'+''.join('<th>'+html.escape(c)+'</th>' for c in cols)+'</tr></thead><tbody>'
                table+=''.join('<tr>'+''.join('<td>'+html.escape(row.get(c,'') or '')+'</td>' for c in cols)+'</tr>' for row in selected)+'</tbody></table>'
                nav='<nav><a href="/">返回首页</a>'
                if page>1:nav+=f'<a href="/orders?page={page-1}">上一页</a>'
                if page<pages:nav+=f'<a href="/orders?page={page+1}">下一页</a>'
                nav+='</nav>'
                return self.reply(200,document('订单查询',f'<h1>订单查询</h1><p>第 {page} 页 / 共 {pages} 页；共 {len(records)} 行。页面为原始记录，可能包含重复及无效记录。</p>'+nav+table),'text/html; charset=utf-8')
            if uri.path.startswith('/downloads/'):
                allowed={'/downloads/orders.csv':'orders','/downloads/refunds.csv':'refunds','/downloads/ledger.csv':'ledger'}
                name=allowed.get(uri.path)
                if name is None:return self.error_json(404,'文件不存在')
                return self.reply(200,(DATA/FILES[name]).read_bytes(),'text/csv; charset=utf-8',{'Content-Disposition':f'attachment; filename="{FILES[name]}"'})
            if uri.path == '/api/refunds':
                page=page_number(q,'page',1,10000); size=page_number(q,'page_size',10,100)
                if q.get('simulate') == ['rate_limit']:
                    key=(self.client_address[0],uri.query)
                    with RATE_LOCK:
                        if key not in RATE_SEEN:
                            RATE_SEEN.add(key)
                            return self.error_json(429,'模拟限流，请按 Retry-After 重试',{'Retry-After':'1'})
                records=rows('refunds'); selected=records[(page-1)*size:page*size]
                next_url=f'/api/refunds?page={page+1}&page_size={size}' if page*size<len(records) else None
                payload={'items':selected,'page':page,'page_size':size,'total':len(records),'next':next_url}
                return self.reply(200,json.dumps(payload,ensure_ascii=False).encode('utf8'),'application/json; charset=utf-8')
            return self.error_json(404,'路径不存在')
        except (ValueError,OSError) as exc:
            return self.error_json(400 if isinstance(exc,ValueError) else 500,'参数不合法或测试资料不可用')

def main():
    port=8765
    if len(sys.argv)>1:
        port=int(sys.argv[1])
        if not 1024<=port<=65535:raise ValueError('端口必须在 1024–65535 之间')
    for name in FILES: rows(name)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'模拟业务系统已启动：http://127.0.0.1:{port}/ （仅本机可访问；关闭窗口停止）',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()

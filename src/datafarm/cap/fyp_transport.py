"""Loopback Responses bridge derived from aspire-agent/tools/aspire_codex_provider.py.

FYP personal credentials stay in the parent process on the authorized machine.
"""
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def load_fyp(path):
    fields = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            k, sep, v = line.partition('=')
            if sep: fields[k.strip()] = v.strip()
    url = fields.get('OPENAI_BASE_URL', '').rstrip('/')
    key = fields.get('OPENAI_API_KEY', '')
    if url != 'https://fyp-nus-gateway.tail5b2dc5.ts.net/v1' or not key.startswith('fyp_'):
        raise ValueError('Expected the original authorized FYP gateway configuration')
    return url, key

class LocalTransport:
    """HTTP/1 bridge for the CLI; personal token never reaches child environment."""
    def __init__(self,url,key):
        self.calls=0;self.url=url.rstrip('/');self.key=key;self.request_shape=None;self.upstream_model=None
        self.client_key=os.urandom(24).hex();owner=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_GET(self):
                if not self.path.startswith('/v1/models'):self.send_error(403);return
                data=json.dumps({'object':'list','data':[{'id':'gpt-6.1-sol','object':'model'}]}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def do_POST(self):
                if self.path!='/v1/responses' or self.headers.get('Authorization')!='Bearer '+owner.client_key:
                    self.send_error(403);return
                size=int(self.headers.get('Content-Length','0'))
                if size>4*1024*1024 or owner.calls>=1:self.send_error(429);return
                owner.calls+=1
                body=self.rfile.read(size)
                document=json.loads(body)
                owner.request_shape={'keys':sorted(document),'tools':[{'type':t.get('type'),'name':t.get('name')} for t in document.get('tools',[])],
                                     'model':document.get('model'),'input_types':[v.get('type') for v in document.get('input',[]) if isinstance(v,dict)]}
                # FYP is stateless: inline inputs only, no server-side tools or
                # Codex prompt-cache/retention extensions. This actor has no tools.
                inline={k:document[k] for k in ('model','input','instructions','stream','reasoning','text') if k in document}
                inline.update(store=False,max_output_tokens=5000)
                body=json.dumps(inline).encode()
                req=Request(owner.url+'/responses',data=body,headers={'Authorization':'Bearer '+owner.key,
                    'Content-Type':'application/json','Accept':'text/event-stream'},method='POST')
                try:
                    response=build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=160)
                    with response:
                        self.send_response(response.status)
                        self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                        self.send_header('Transfer-Encoding','chunked');self.end_headers()
                        pending=b''
                        while chunk:=response.read1(8192):
                            if owner.upstream_model is None:
                                pending+=chunk
                                while b'\n' in pending:
                                    line,pending=pending.split(b'\n',1)
                                    if line.startswith(b'data:'):
                                        try:
                                            event=json.loads(line[5:]);owner.upstream_model=event.get('response',{}).get('model')
                                        except (ValueError,AttributeError):pass
                            self.wfile.write(f'{len(chunk):X}\r\n'.encode()+chunk+b'\r\n');self.wfile.flush()
                        self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                except HTTPError as error:
                    data=error.read(16000);self.send_response(error.code)
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except Exception:
                    self.close_connection=True
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

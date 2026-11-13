"""CaP execution over the existing author-visible xh file protocol.

The broker still owns source audit, seed authorization, isolation, jobs and judge.
This client never launches a simulator or opens the harness-private directory.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from .io import digest, read_json, source_digest, write_json, write_source


class XHEngine:
    def __init__(self, round_dir, expert_class, trace_relative, *, timeout_s=600, poll_s=.25):
        self.round = Path(round_dir).resolve()
        if any(not (self.round/'broker'/name).is_dir() for name in ('requests','responses','results')):
            raise ValueError('Use a prepared expert-harness round with public broker directories')
        self.workspace = self.round/'workspace'
        self.cache = self.workspace/'cap-state'
        self.cache.mkdir(parents=True,exist_ok=True)
        if not isinstance(expert_class,str) or not expert_class.isidentifier():
            raise ValueError('expert_class must be a Python identifier')
        trace = Path(trace_relative)
        if trace.is_absolute() or '..' in trace.parts or not trace.parts:
            raise ValueError('trace_relative must stay within each public rollout directory')
        if not 0 < timeout_s <= 86400 or not 0 < poll_s <= 5:
            raise ValueError('Invalid broker wait budget')
        self.expert_class,self.trace_relative = expert_class,trace
        self.timeout_s,self.poll_s = timeout_s,poll_s
        self.episodes = {}

    def _request(self, kind, args):
        identity = digest({'kind':kind,'args':args})
        rid = 'cap-'+identity[:32]
        intent = self.cache/(rid+'.json')
        if not intent.exists():
            write_json(intent,{'schema':'datafarm.expert-harness.request/1','request_id':rid,
                               'kind':kind,'args':args,
                               'created_at':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
        body=read_json(intent)
        if body.get('request_id')!=rid or body.get('kind')!=kind or body.get('args')!=args:
            raise ValueError('Persisted broker intent was modified')
        request = self.round/'broker/requests'/(rid+'.json')
        response = self.round/'broker/responses'/(rid+'.json')
        if not request.exists() and not response.exists():
            write_json(request,body)
        deadline = time.monotonic()+self.timeout_s
        while time.monotonic() < deadline:
            if response.exists():
                answer = read_json(response)
                if answer.get('request_id') != rid or answer.get('status') != 'accepted':
                    raise ValueError('Expert broker refused the request: '+str(answer.get('reason','invalid response')))
                return answer
            time.sleep(self.poll_s)
        raise TimeoutError('Broker request remains queued under stable ID '+rid+'; query/resume it rather than resubmit')

    def _source(self, code):
        name = 'cap-'+source_digest(code)+'.py'
        target = self.workspace/name
        if target.exists() and source_digest(target.read_text(encoding='utf-8')) != source_digest(code):
            raise ValueError('Content-addressed expert source was modified')
        if not target.exists():write_source(target,code)
        return name

    def _result(self, attempt_id):
        if not isinstance(attempt_id,str) or not re.fullmatch(r'A-[0-9]+',attempt_id):
            raise ValueError('Invalid broker attempt ID')
        target=self.round/'broker/results'/(attempt_id+'.json')
        deadline=time.monotonic()+self.timeout_s
        while time.monotonic()<deadline:
            if target.exists():return read_json(target)
            time.sleep(self.poll_s)
        raise TimeoutError('Rollout '+attempt_id+' remains pending; its broker identity is preserved')

    def _trace(self, raw):
        directory=Path(raw['run_dir']).resolve()
        public_rollouts=(self.round/'rollouts').resolve()
        if not directory.is_relative_to(public_rollouts):
            raise ValueError('Broker result did not identify a public rollout directory')
        trace=(directory/self.trace_relative).resolve()
        if not trace.exists() and raw.get('outcome_class')=='expert_error':
            return []  # A counted import/runtime failure can precede the first primitive.
        if not trace.is_relative_to(directory) or trace.stat().st_size>8*1024*1024:
            raise ValueError('Public trace is outside the rollout or exceeds the evidence budget')
        records=[json.loads(s) for s in trace.read_text(encoding='utf-8').splitlines() if s.strip()]
        records=[r for r in records if r.get('event')=='primitive']
        ids=[r.get('event_id') for r in records]
        if (any(type(i) is not int or i<0 for i in ids) or len(set(ids)) != len(ids)
                or any(not isinstance(r.get('api'),str) for r in records)):
            raise ValueError('Primitive trace has invalid or duplicate event IDs')
        return records

    def evaluate(self, code, seeds, phase):
        name=self._source(code);episodes=[]
        for seed in seeds:
            reply=self._request('rollout',{'seed':seed,'file':name,'class':self.expert_class,
                                          'note':'CaP '+phase,'hypothesis':'candidate '+source_digest(code)})
            raw=self._result(reply['attempt_id'])
            if (raw.get('attempt_id') != reply['attempt_id'] or raw.get('seed') != seed
                    or raw.get('expert_sha256') != source_digest(code)):
                raise ValueError('Broker outcome is not bound to the requested expert and seed')
            if raw.get('counted') is not True or type(raw.get('success')) is not bool:
                raise RuntimeError('Infrastructure outcome cannot be treated as a program failure: '+str(raw.get('outcome_class')))
            trace=self._trace(raw)
            episode={'episode_id':raw['attempt_id'],'phase':phase,'seed':seed,'success':raw['success'],
                     'score':float(raw['success']), 'evaluation':{'reason':raw.get('detail','')},
                     'trace':[{'event_id':r['event_id'],'api':r['api'],
                               'error':r.get('error'),'result':r.get('result')} for r in trace]}
            self.episodes[raw['attempt_id']]=trace;episodes.append(episode)
        return {'episodes':episodes}

    def inspect(self, episode_id, event_ids):
        records=self.episodes[episode_id]
        selected=[r for r in records if r['event_id'] in event_ids]
        if {r['event_id'] for r in selected} != set(event_ids):
            raise ValueError('Unknown primitive event ID')
        if len(str(selected))>24000:
            raise ValueError('Selected evidence exceeds the context budget; inspect fewer events')
        return selected

    def submit(self, code):
        return self._request('submit',{'file':self._source(code),'class':self.expert_class,
                                        'note':'CaP development-selected frozen expert; hidden holdout remains pending',
                                        'final':True})


def build_engine(config):
    return XHEngine(**config)

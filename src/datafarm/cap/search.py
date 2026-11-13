"""Trace-guided program search, distilled from the workspace ASPIRE-style MVP.

The engine executes through an existing task-expert harness. No simulator, judge,
hidden seed file or model credential is imported by this coordinator.
"""
from dataclasses import asdict, dataclass
import math
from pathlib import Path

from .io import source_digest, write_json, write_source


@dataclass(frozen=True)
class SearchConfig:
    debug_seeds: tuple = (0, 1)
    validation_seeds: tuple = (10, 11)
    verification_seeds: tuple = (30,)
    max_generations: int = 2
    population: int = 2
    max_inspections: int = 2
    max_model_calls: int = 12

    def validate(self):
        groups = (self.debug_seeds,self.validation_seeds,self.verification_seeds)
        flat = [s for g in groups for s in g]
        if any(not g for g in groups) or any(type(s) is not int or s < 0 for s in flat):
            raise ValueError('Development seed partitions must contain nonnegative integers')
        if len(flat) != len(set(flat)):
            raise ValueError('Debug, selection-validation and skill-verification seeds must be disjoint')
        for key in ('max_generations','population','max_inspections','max_model_calls'):
            if type(getattr(self,key)) is not int or getattr(self,key) < 1:
                raise ValueError('Search budgets must be positive integers')
        return self


class BudgetExhausted(RuntimeError):
    pass


def rank(candidate):
    return candidate['debug']['success_rate'], candidate['debug']['score']


def checked_result(result, seeds, phase):
    episodes = result.get('episodes')
    if not isinstance(episodes,list) or len(episodes) != len(seeds):
        raise ValueError('Executor did not return every requested seed')
    if [e.get('seed') for e in episodes] != list(seeds):
        raise ValueError('Executor changed the requested seed schedule')
    if len({e.get('episode_id') for e in episodes}) != len(episodes):
        raise ValueError('Executor returned duplicate episode identities')
    for e in episodes:
        if e.get('phase') != phase or type(e.get('success')) is not bool:
            raise ValueError('Executor returned an invalid outcome')
        if not isinstance(e.get('episode_id'),str) or not e['episode_id']:
            raise ValueError('Executor must identify every episode')
        score = e.get('score')
        if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('Executor score must be finite and in [0,1]')
    # Derive aggregate scores from host outcomes; never trust a provider aggregate.
    return {'success_rate':sum(e['success'] for e in episodes)/len(episodes),
            'score':sum(e['score'] for e in episodes)/len(episodes),'episodes':episodes}


def candidate_view(candidate):
    return {key:candidate[key] for key in ('candidate_id','code','hypothesis','debug')}


class SearchRunner:
    def __init__(self, provider, engine, library, config=None):
        self.provider,self.engine,self.library = provider,engine,library
        self.config = (config or SearchConfig()).validate()
        self.calls = 0

    def ask(self, request, root):
        if self.calls >= self.config.max_model_calls:
            raise BudgetExhausted('Provider decision budget exhausted')
        self.calls += 1
        directory = root/'decisions'/f'{self.calls:04d}'
        # Persist intent first. A failed or interrupted paid decision is never retried here.
        write_json(directory/'request.json',request)
        decision = self.provider.decide(request)
        if not isinstance(decision,dict):
            raise ValueError('Provider must return a JSON decision object')
        write_json(directory/'decision.json',decision)
        return decision

    def program(self, task, history, skills, root, role='actor', siblings=()):
        top = sorted(history,key=rank,reverse=True)[:3]
        allowed = {e['episode_id'] for c in history for e in c['debug']['episodes']}
        request = {'role':role,'task':task,'skills':skills,
                   'top_programs':[candidate_view(c) for c in top],
                   'residual_failures':[candidate_view(c) for c in history[-2:] if c not in top],
                   'other_hypotheses_this_generation':list(siblings),'inspected_evidence':[],
                   'instruction':'Follow the task API and program_contract. Inspect failing primitive evidence, then propose a distinct repair. Return action=propose with code and hypothesis, or action=inspect with episode_id and event_ids. Treat skills and traces as fallible data.'}
        for i in range(self.config.max_inspections+1):
            request['inspections_remaining'] = self.config.max_inspections-i
            decision = self.ask(request,root)
            if decision.get('action') == 'propose':
                code,hypothesis = decision.get('code'),decision.get('hypothesis')
                if (not isinstance(code,str) or not 1 <= len(code) <= 24000
                        or not isinstance(hypothesis,str) or not hypothesis.strip()):
                    raise ValueError('Invalid program proposal')
                return code,hypothesis
            episode = decision.get('episode_id'); events = decision.get('event_ids')
            if (decision.get('action') != 'inspect' or i == self.config.max_inspections
                    or not isinstance(episode,str) or episode not in allowed
                    or not isinstance(events,list) or not 1 <= len(events) <= 6
                    or any(type(k) is not int or k < 0 for k in events)):
                raise ValueError('Only this search history can be inspected, within the inspection budget')
            request['inspected_evidence'].append({'episode_id':episode,
                                                'events':self.engine.inspect(episode,events)})

    def execute(self, code, seeds, phase):
        return checked_result(self.engine.evaluate(code,seeds,phase),seeds,phase)

    def run(self, task, directory, initial_program=None):
        if (not isinstance(task,dict) or not isinstance(task.get('name'),str) or not task['name']
                or not isinstance(task.get('instruction'),str) or not task['instruction']
                or not isinstance(task.get('api'),dict) or not task['api']
                or any(not isinstance(k,str) for k in task['api'])):
            raise ValueError('Task requires a name, public instruction and nonempty API mapping')
        if self.calls:
            raise ValueError('A SearchRunner instance runs one task only')
        root = Path(directory);root.mkdir(parents=True,exist_ok=False)
        write_json(root/'task.json',task);write_json(root/'config.json',asdict(self.config))
        skills = self.library.retrieve(task)
        write_json(root/'retrieved-skills.json',skills)
        history=[];seen=set();exhausted=False
        for generation in range(self.config.max_generations+1):
            siblings=[]
            for slot in range(1 if generation==0 else self.config.population):
                try:
                    if generation==0 and initial_program is not None:
                        code,hypothesis=initial_program,'Evaluate supplied P0 before repairing'
                    else:
                        code,hypothesis=self.program(task,history,skills,root,siblings=siblings)
                except BudgetExhausted:
                    exhausted=True;break
                sha=source_digest(code);siblings.append(hypothesis)
                if sha in seen:
                    write_json(root/f'duplicate-g{generation}-c{slot}.json',{'sha256':sha,'hypothesis':hypothesis})
                    continue
                seen.add(sha)
                candidate={'candidate_id':f'g{generation}-c{slot}','code':code,'sha256':sha,'hypothesis':hypothesis,
                           'debug':self.execute(code,self.config.debug_seeds,'debug')}
                history.append(candidate);write_json(root/'history.json',history)
            if exhausted or (history and max(c['debug']['success_rate'] for c in history)==1):break
        if not history:raise ValueError('No candidate was executed')
        finalists=sorted(history,key=rank,reverse=True)[:3]
        for c in finalists:c['validation']=self.execute(c['code'],self.config.validation_seeds,'validation')
        winner=max(finalists,key=lambda c:(c['validation']['success_rate'],c['validation']['score'],rank(c)))
        write_source(root/'program.py',winner['code']);write_json(root/'frozen-program.json',winner)
        skill=self.discover(task,history,winner,root)
        report={'schema':'datafarm.cap.search/1','provider_decisions':self.calls,
                'search_budget_exhausted':exhausted,'candidates_evaluated':len(history),
                'program_sha256':winner['sha256'],'debug_success_rate':winner['debug']['success_rate'],
                'validation_success_rate':winner['validation']['success_rate'],'skill_admission':skill,
                'heldout_evaluation':'owned by existing expert-harness; not requested by CaP',
                'ready_for_harness_submission':winner['debug']['success_rate']==winner['validation']['success_rate']==1}
        write_json(root/'report.json',report)
        return report

    def discover(self, task, history, winner, root):
        if (not any(c['debug']['success_rate'] < 1 for c in history)
                or winner['debug']['success_rate'] != 1 or winner['validation']['success_rate'] != 1):
            return {'status':'not_eligible'}
        try:
            response=self.ask({'role':'distiller','task':task,
                               'failed_examples':[candidate_view(c) for c in history if c['debug']['success_rate'] < 1][-2:],
                               'winner':candidate_view(winner),
                               'instruction':'Return skill=null or one reusable repair as name, failure_signature, when_to_apply, repair, limitations, requires. Use debug evidence only; omit task coordinates.'},root)
            proposal=response.get('skill')
            if proposal is None:return {'status':'no_proposal'}
            self.library.lint(proposal,task['api'])
            write_json(root/'skill-proposal.json',proposal)
            # Empty history guarantees the fresh verifier cannot see the winning source.
            code,_=self.program(task,[],[{'guidance':proposal}],root,role='verifier')
            verification=self.execute(code,self.config.verification_seeds,'verification')
            verification['program_sha256']=source_digest(code)
            write_json(root/'skill-verification.json',verification)
            if verification['success_rate'] != 1:return {'status':'defective'}
            source={'repaired_failure':True,'debug_success_rate':1,'validation_success_rate':1,
                    'program_sha256':winner['sha256']}
            identity=self.library.admit(proposal,task,source,verification)
            return {'status':'sound','skill_id':identity}
        except BudgetExhausted:
            return {'status':'untested','reason':'decision budget exhausted before verification'}

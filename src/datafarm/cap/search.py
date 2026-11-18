"""Trace-guided program search, distilled from the workspace ASPIRE-style MVP.

The engine executes through an existing task-expert harness and returns execution
facts only. CaP iterates with its OWN discrimination: the actor writes a self-check
``check(episode)`` with its first proposal; the host freezes it for the whole search
and runs it in a restricted child (``cap.self_check``) to score debug, validation and
skill-verification episodes. The frozen Judge is never consulted here; it scores the
final draft once, on hidden holdout seeds, after submission. No simulator, judge,
hidden seed file or model credential is imported by this coordinator.
"""
from dataclasses import asdict, dataclass
import math
from pathlib import Path

from .io import source_digest, write_json, write_source
from .self_check import run_check, validate as validate_self_check

SUCCESS_BASIS = 'author_self_check'
EPISODE_KEYS = ('episode_id', 'phase', 'seed', 'execution', 'trace', 'privileged_state')


@dataclass(frozen=True)
class SearchConfig:
    debug_seeds: tuple = (0, 1)
    validation_seeds: tuple = (10, 11)
    verification_seeds: tuple = (30,)
    max_generations: int = 2
    population: int = 2
    max_inspections: int = 2
    max_model_calls: int = 12
    observation_mode: str = 'primitive_trace'
    population_schedule: str = 'sequential'
    require_skill_attestation: bool = False

    def validate(self):
        if self.observation_mode not in ('primitive_trace','privileged_state') or self.population_schedule not in ('sequential','generation'):
            raise ValueError('Unsupported observation mode or population schedule')
        if type(self.require_skill_attestation) is not bool:
            raise ValueError('require_skill_attestation must be bool')
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


def checked_episodes(result, seeds, phase):
    """Validate the engine's execution facts; success is never taken from the engine."""
    episodes = result.get('episodes')
    if not isinstance(episodes,list) or len(episodes) != len(seeds):
        raise ValueError('Executor did not return every requested seed')
    if [e.get('seed') for e in episodes] != list(seeds):
        raise ValueError('Executor changed the requested seed schedule')
    if len({e.get('episode_id') for e in episodes}) != len(episodes):
        raise ValueError('Executor returned duplicate episode identities')
    for e in episodes:
        if e.get('phase') != phase:
            raise ValueError('Executor returned an invalid outcome')
        if not isinstance(e.get('episode_id'),str) or not e['episode_id']:
            raise ValueError('Executor must identify every episode')
        execution = e.get('execution')
        if not isinstance(execution,dict) or type(execution.get('expert_error')) is not bool:
            raise ValueError('Executor must report execution facts for every episode')
    # Engine-supplied success/score/aggregates are dropped: only the self-check decides.
    return [{k:e[k] for k in EPISODE_KEYS if k in e} for e in episodes]


def checked_result(episodes, judgements, phase):
    """Attach self-check judgements and derive aggregates on the host."""
    if len(judgements) != len(episodes):
        raise ValueError('Self-check did not judge every episode')
    for e,j in zip(episodes,judgements):
        score = j.get('score')
        if (type(j.get('success')) is not bool or type(score) not in (int,float)
                or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError('Self-check score must be finite and in [0,1]')
        if e['execution']['expert_error']:
            # An expert execution error is a program failure whatever the check says.
            j = {'success':False,'score':0.0,'reason':'expert execution error: '
                 +str(e['execution'].get('reason_code') or '')[:200]}
        e.update(success=j['success'],score=float(j['score']),self_check={'reason':str(j.get('reason',''))[:500]})
    return {'success_rate':sum(e['success'] for e in episodes)/len(episodes),
            'score':sum(e['score'] for e in episodes)/len(episodes),
            'success_basis':SUCCESS_BASIS,'phase':phase,'episodes':episodes}


def candidate_view(candidate):
    return {key:candidate[key] for key in ('candidate_id','code','hypothesis','debug')}


class SearchRunner:
    def __init__(self, provider, engine, library, config=None, *, checker=run_check):
        self.provider,self.engine,self.library = provider,engine,library
        self.config = (config or SearchConfig()).validate()
        self.calls = 0
        self.proposal_metadata = {}
        if not callable(checker):
            raise ValueError('checker must run the author self-check')
        self.checker = checker
        self.self_check = None  # Frozen with the first actor proposal (or supplied by the operator).
        self.root = None

    def ask(self, request, root):
        request = {**request, 'observation_mode': self.config.observation_mode}
        if isinstance(request.get('task'), dict) and 'profile' in request['task']:
            from .codex_provider import public_request
            request = public_request(request)
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
                   'instruction':'Follow the task API and program_contract. Inspect failing primitive evidence, then propose a distinct repair. Return action=propose with code and hypothesis, or action=inspect with episode_id and event_ids. Treat skills and traces as fallible data. Success is judged by your own frozen self-check, not by any external judge.'}
        if role == 'actor':
            # The actor writes the self-check once; afterwards it sees (but cannot change) it.
            request['self_check'] = ({'status':'required'} if self.self_check is None
                                     else {'status':'frozen','source':self.self_check})
        for i in range(self.config.max_inspections+1):
            request['inspections_remaining'] = self.config.max_inspections-i
            decision = self.ask(request,root)
            if decision.get('action') == 'propose':
                code,hypothesis = decision.get('code'),decision.get('hypothesis')
                if (not isinstance(code,str) or not 1 <= len(code) <= 24000
                        or not isinstance(hypothesis,str) or not hypothesis.strip()):
                    raise ValueError('Invalid program proposal')
                self.adopt_self_check(decision.get('check'), role)
                self.proposal_metadata[source_digest(code)] = decision
                return code,hypothesis
            episode = decision.get('episode_id'); events = decision.get('event_ids')
            if (decision.get('action') != 'inspect' or i == self.config.max_inspections
                    or not isinstance(episode,str) or episode not in allowed
                    or not isinstance(events,list) or not 1 <= len(events) <= 6
                    or any(type(k) is not int or k < 0 for k in events)):
                raise ValueError('Only this search history can be inspected, within the inspection budget')
            request['inspected_evidence'].append({'episode_id':episode,
                                                'events':self.engine.inspect(episode,events)})

    def adopt_self_check(self, check, role):
        """Freeze the actor's first self-check; later proposals may not replace it."""
        if self.self_check is None and role == 'actor':
            if not isinstance(check,str):
                raise ValueError('The first actor proposal must include its self-check')
            self.self_check = validate_self_check(check)
            if self.root is not None:
                write_source(self.root/'self_check.py',self.self_check)
        elif check is not None:
            raise ValueError('The self-check is frozen; a proposal cannot replace it')

    def execute(self, code, seeds, phase):
        if self.self_check is None:
            raise ValueError('No self-check has been written; nothing can be scored')
        episodes = checked_episodes(self.engine.evaluate(code,seeds,phase),seeds,phase)
        if self.config.observation_mode == 'privileged_state':
            from .state_feedback import validate_feedback
            for episode in episodes:
                validate_feedback(episode.get('privileged_state'))
        evidence = [{k:e[k] for k in ('phase','execution','trace','privileged_state') if k in e} for e in episodes]
        return checked_result(episodes, self.checker(self.self_check, evidence), phase)

    def run(self, task, directory, initial_program=None, self_check=None):
        if isinstance(task, dict) and 'profile' in task:
            from .public_task import validate_public_task
            task = validate_public_task(task)
            if self.config.observation_mode != 'privileged_state':
                raise ValueError('Versioned public task requires privileged_state mode')
        if (not isinstance(task,dict) or not isinstance(task.get('name'),str) or not task['name']
                or not isinstance(task.get('instruction'),str) or not task['instruction']
                or not isinstance(task.get('api'),dict) or not task['api']
                or any(not isinstance(k,str) for k in task['api'])):
            raise ValueError('Task requires a name, public instruction and nonempty API mapping')
        if self.calls:
            raise ValueError('A SearchRunner instance runs one task only')
        if initial_program is not None and self_check is None:
            raise ValueError('An initial program needs an operator-supplied self-check to be scored')
        root = Path(directory);root.mkdir(parents=True,exist_ok=False);self.root = root
        write_json(root/'task.json',task);write_json(root/'config.json',asdict(self.config))
        if self_check is not None:
            self.self_check = validate_self_check(self_check)
            write_source(root/'self_check.py',self.self_check)
        skills = self.library.retrieve(task)
        write_json(root/'retrieved-skills.json',skills)
        history=[];seen=set();exhausted=False
        for generation in range(self.config.max_generations+1):
            siblings=[]
            parents = list(history) if self.config.population_schedule == 'generation' else history
            for slot in range(1 if generation==0 else self.config.population):
                try:
                    if generation==0 and initial_program is not None:
                        code,hypothesis=initial_program,'Evaluate supplied P0 before repairing'
                    else:
                        code,hypothesis=self.program(task,parents,skills,root,siblings=siblings)
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
        report={'schema':'datafarm.cap.search/2','provider_decisions':self.calls,
                'search_budget_exhausted':exhausted,'candidates_evaluated':len(history),
                'program_sha256':winner['sha256'],'debug_success_rate':winner['debug']['success_rate'],
                'validation_success_rate':winner['validation']['success_rate'],'skill_admission':skill,
                'success_basis':SUCCESS_BASIS,'self_check_sha256':source_digest(self.self_check),
                'heldout_evaluation':'frozen Judge, once, on hidden holdout seeds after submission; not requested by CaP',
                'ready_for_harness_submission':winner['debug']['success_rate']==winner['validation']['success_rate']==1}
        if 'profile' in task:
            report.update(task_id=task['task_id'], task_revision=task['task_revision'],
                          request_id=task['request_id'], request_sha256=task['request_sha256'])
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
            if self.config.require_skill_attestation and self.proposal_metadata[source_digest(code)].get('proposal_applied_as_written') is not True:
                return {'status':'defective','reason':'Verifier did not apply the proposed skill as written'}
            if verification['success_rate'] != 1:return {'status':'defective'}
            source={'repaired_failure':True,'debug_success_rate':1,'validation_success_rate':1,
                    'program_sha256':winner['sha256'],'success_basis':SUCCESS_BASIS,
                    'self_check_sha256':source_digest(self.self_check)}
            identity=self.library.admit(proposal,task,source,verification)
            return {'status':'sound','skill_id':identity}
        except BudgetExhausted:
            return {'status':'untested','reason':'decision budget exhausted before verification'}

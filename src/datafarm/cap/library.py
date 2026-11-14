"""Content-addressed reusable repair guidance, separate from whole-expert admission."""
from pathlib import Path

from .io import digest, read_json, write_json

FIELDS = {'name', 'failure_signature', 'when_to_apply', 'repair', 'limitations', 'requires'}


class SkillLibrary:
    def __init__(self, root):
        self.root = Path(root)

    def entries(self):
        result = []
        for filename in sorted(self.root.glob('*.json')):
            entry = read_json(filename)
            if (set(entry) != {'schema','id','payload'} or entry['schema'] != 'datafarm.cap.skill/1'
                    or digest(entry['payload']) != entry['id'] or filename.stem != entry['id']):
                raise ValueError('Skill integrity check failed: '+filename.name)
            result.append(entry['payload'])
        return result

    @staticmethod
    def lint(proposal, api):
        if not isinstance(proposal, dict) or set(proposal) != FIELDS:
            raise ValueError('Skill proposal must contain exactly the six guidance fields')
        for key in FIELDS-{'requires'}:
            if not isinstance(proposal[key],str) or not 1 <= len(proposal[key]) <= 1800:
                raise ValueError('Skill guidance must be bounded, nonempty text')
        requires = proposal['requires']
        if (not isinstance(requires,list) or not requires or any(not isinstance(k,str) for k in requires)
                or not set(requires) <= set(api)):
            raise ValueError('Skill requires an unavailable API')

    def retrieve(self, task, limit=6):
        words = set((task['name']+' '+task['instruction']).lower().split())
        eligible = [e for e in self.entries() if set(e['guidance']['requires']) <= set(task['api'])]
        eligible.sort(key=lambda e: (-len(words & set(str(e['guidance']).lower().split())), digest(e)))
        return eligible[:limit]

    def admit(self, proposal, task, source, verification):
        self.lint(proposal, task['api'])
        episodes = verification['episodes']
        if (source['repaired_failure'] is not True or source['debug_success_rate'] != 1
                or source['validation_success_rate'] != 1 or not episodes
                or verification['success_rate'] != 1
                or any(e['success'] is not True or e['phase'] != 'verification' for e in episodes)):
            raise ValueError('Skill admission requires measured repair and fresh successful verification')
        payload = {'guidance':proposal,'source_task':task['name'], 'source':source,
                   'verification':verification}
        identity = digest(payload)
        write_json(self.root/(identity+'.json'), {'schema':'datafarm.cap.skill/1','id':identity,'payload':payload})
        return identity

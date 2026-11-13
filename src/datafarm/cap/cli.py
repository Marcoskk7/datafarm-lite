"""Optional author-side CaP runner; all physical execution stays behind the xh broker."""
import argparse
import importlib
import json
from pathlib import Path

from .io import read_json, write_json
from .library import SkillLibrary
from .search import SearchConfig, SearchRunner


def factory(spec, config):
    module,sep,name=spec.partition(':')
    if not sep or not module or not name.isidentifier():
        raise ValueError('Factory must be an operator-selected module:function')
    return getattr(importlib.import_module(module),name)(config)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task',required=True,type=Path)
    p.add_argument('--config',required=True,type=Path)
    p.add_argument('--engine-config',required=True,type=Path)
    p.add_argument('--engine-factory',default='datafarm.cap.xh:build_engine')
    p.add_argument('--provider-config',type=Path)
    p.add_argument('--provider-factory',required=True,
                   help='Deployment factory for stateless decisions through its existing model/cost runtime')
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--library',required=True,type=Path)
    p.add_argument('--initial-program',type=Path)
    p.add_argument('--submit',action='store_true',help='Submit only after every development-selection check passes')
    args=p.parse_args(argv)
    try:
        config=SearchConfig(**read_json(args.config)).validate()
        engine=factory(args.engine_factory,read_json(args.engine_config))
        provider=factory(args.provider_factory,read_json(args.provider_config) if args.provider_config else {})
        initial=args.initial_program.read_text(encoding='utf-8') if args.initial_program else None
        report=SearchRunner(provider,engine,SkillLibrary(args.library),config).run(read_json(args.task),args.output,initial)
        if args.submit:
            if not report['ready_for_harness_submission']:
                raise ValueError('Expert failed development selection; final submission refused')
            acknowledgement=engine.submit((args.output/'program.py').read_text(encoding='utf-8'))
            write_json(args.output/'submission.json',acknowledgement)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if report['ready_for_harness_submission'] else 2
    except (ValueError,RuntimeError,OSError,TypeError) as exc:
        p.exit(2,f'{type(exc).__name__}: {exc}\n')


if __name__=='__main__':raise SystemExit(main())

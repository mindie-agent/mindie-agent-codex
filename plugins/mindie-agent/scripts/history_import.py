"""Manual historical contribution. Never imported by Stop or maintenance.

Runs in the committed interpreter under the bridge's generation lock. The
current native task is associated internally. Saved contribution
consent is reused without changing any choice, scope, or other task's lease.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path

from admission_ops import admission, native_session, read_config


class ConfigurationError(ValueError):
    """Static diagnostics for missing parts of the Codex import pipeline."""


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True,
                        help='one user-selected native Codex JSONL file; repeat for more files')
    parser.add_argument('--retry-summary', action='store_true',
                        help='explicitly retry failed/unknown index calls; unknown calls may incur another charge')
    args = parser.parse_args(argv)
    for source in args.source:
        if not Path(source).is_absolute():
            parser.error('--source must be an absolute file path')
    return args


def run_imports(sources, *, emit, retry_summary=False):
    # Check these BEFORE even importing the parser, opening a source, or
    # creating a knowledge store. An explicit import is not a history scan or
    # a change to the installation's contribution choice.
    config = read_config()
    authority = admission(config)
    session = native_session()
    import sharing
    settings = sharing.read()
    inspected = authority.inspect(session)
    if inspected.get('status') == 'active':
        lease = authority.check(session)
    elif inspected.get('status') == 'missing':
        candidate = dict(project_root=str(Path.cwd().resolve()), root_session=session,
                         activated_at=(settings or {}).get('enabled_at'))
        if not sharing.capture_allowed(candidate, None):
            raise ValueError('history import requires enabled contribution in this task scope')
        lease = authority.associate(session, project_root=candidate['project_root'],
                                    not_before=candidate['activated_at'])
    else:
        raise ValueError('current native task association is revoked or unavailable')
    if not sharing.capture_allowed(lease, None):
        raise ValueError('history import requires enabled contribution in this task scope')
    from mindie_knowledge.loop.cli import config_at, ensure_service, load_transcript_adapter
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.history_import import HistoryImportError, import_transcript
    from mindie_knowledge.loop.store import Store
    from mindie_knowledge.loop import settings as settings_mod

    engine_config = config_at(config['engine_config'])
    if engine_config.get('admission_path') != config['admission_path']:
        raise ValueError('history import requires the configured admission store')
    if engine_config.get('capture_mode') != 'public-transcript':
        raise ValueError('history import requires public-transcript mode')
    summary_command = engine_config.get('summary_command')
    if (not isinstance(summary_command, list) or not summary_command
            or not all(isinstance(part, str) and part for part in summary_command)):
        raise ConfigurationError('history import requires the configured summary worker; source excerpts are not a substitute')
    parser = load_transcript_adapter(engine_config)
    if not callable(getattr(parser, 'history_source', None)):
        raise ValueError('the installed transcript adapter does not support history import')
    failed = changed = accepted = 0
    with closing(Store(engine_config['root'], engine_config['domain'])) as store:
        engine = Engine(store, settings_path=engine_config.get('community_config'),
                        admission=authority, transcript_adapter=parser,
                        capture_mode='public-transcript',
                        summary_command=summary_command,
                        redactor_executable=engine_config['redactor_executable'])
        generation = engine._settings().generation
        for source in sources:
            try:
                # Re-check per file, so revocation between files reads no next
                # transcript. No source can expand the installation's scope.
                current = authority.check(session, lease['token'])
                settings = settings_mod.load(engine.settings_path)
                if (not sharing.capture_allowed(current, None)
                        or not settings.allows_capture() or settings.generation != generation):
                    raise HistoryImportError('contribution permission changed during import')
                info = parser.history_source(source)
                result = import_transcript(
                    engine, session_id=session, token=lease['token'], source=source,
                    source_session=info['session_id'], source_scope=info['project_root'],
                    identity=info['identity'], namespace='codex',
                    retry_summary=retry_summary,
                )
                if result['status'] in {'imported', 'extended', 'unchanged'}:
                    summary = result.get('summary') or dict(status='missing')
                    if summary.get('status') in {'missing', 'failed', 'outcome_unknown', 'cancelled'}:
                        failed += 1
                    result['summary'] = summary
                changed += result['status'] in {'imported', 'extended'}
                accepted += result['status'] in {'imported', 'extended', 'unchanged'}
            except Exception as exc:
                failed += 1
                # Never echo parser/scanner exception text: it can contain
                # private source data. Our own refusal messages are static.
                result = dict(status='failed', error=type(exc).__name__)
                if isinstance(exc, HistoryImportError):
                    result['detail'] = str(exc)
            emit(dict(source=source, **result))
    if accepted:
        # Ordinary outbox delivery only. It never re-opens historical sources;
        # an explicit repeat can also prepare delivery after an earlier local
        # save succeeded but service startup failed.
        try:
            current = authority.check(session, lease['token'])
            if not sharing.capture_allowed(current, None):
                raise ValueError('contribution permission changed')
            ensure_service(config['engine_config'])
            emit(dict(publication='pending', service='ready', changed=changed))
        except Exception as exc:
            receipt = dict(publication='pending', service='unavailable', changed=changed,
                           stage='service-start', error=type(exc).__name__)
            for name in ('errno', 'winerror'):
                value = getattr(exc, name, None)
                if type(value) is int:
                    receipt[name] = value
            emit(receipt)
            failed += 1
    return 1 if failed else 0


def main(argv=None):
    args = arguments(argv)
    emit = lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        return run_imports(args.source, emit=emit, retry_summary=args.retry_summary)
    except Exception as exc:
        emit(dict(status='not-started', error=type(exc).__name__,
                  detail=str(exc) if isinstance(exc, ConfigurationError) else
                  'Current native task, saved contribution scope or runtime configuration is unavailable.'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

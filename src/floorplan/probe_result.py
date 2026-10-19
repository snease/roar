"""Validate the isolated ALIGN check's result without Qt or ALIGN imports."""
import json


def parse_probe_result(stdout, *, stderr='', returncode=0, normal_exit=True,
                       timed_out=False, start_error=''):
    """Return a displayable record; process failure always overrides success."""
    def failure(message, detail=''):
        return {'ok': False, 'message': message + ('\n' + detail.strip()[-2500:] if detail.strip() else '')}

    if timed_out:
        return failure('ALIGN check exceeded 30 seconds and was stopped. Check the selected installation and retry.')
    if start_error:
        return failure('ALIGN check could not start. Check the selected Python in ALIGN settings.', start_error)
    lines = [line[len('ROAR_PROBE '):] for line in stdout.splitlines() if line.startswith('ROAR_PROBE ')]
    try:
        result = json.loads(lines[-1]) if lines else None
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            raise ValueError('Expected an object with a boolean ok field')
        for name in ('message', 'python', 'align', 'dash'):
            if name in result and not isinstance(result[name], str):
                raise ValueError(f'{name} must be text')
        if 'browser_viewer' in result and type(result['browser_viewer']) is not bool:
            raise ValueError('browser_viewer must be boolean')
    except (ValueError, TypeError, RecursionError) as error:
        return failure('Invalid installation-check response. Check the selected installation and retry. '
                       + str(error), stderr or stdout)
    if not normal_exit or returncode != 0:
        message = (f'ALIGN check failed with exit code {returncode}.' if normal_exit
                   else f'ALIGN check crashed (exit code {returncode}).')
        # A successful-looking record from a failed process must not advertise
        # readiness. Preserve genuine worker failures and native stderr instead.
        return failure(message, (result.get('message', '') if not result['ok'] else '') or stderr)
    result = result.copy()
    for name in ('message', 'python', 'align', 'dash'):
        if name in result:
            result[name] = result[name][:2500 if name == 'message' else 100]
    result.setdefault('message', 'Installation check passed.' if result['ok'] else 'Installation check failed without a diagnostic.')
    return result

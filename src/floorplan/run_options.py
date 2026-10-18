"""Validate detached ALIGN overrides without importing Qt or ALIGN."""
from copy import deepcopy
import json
from pathlib import Path


PROFILE = json.loads(Path(__file__).with_name('align_profile.json').read_text(encoding='utf-8'))
OPTION_LIMITS = {
    'nvariants': (1, 100),
    'placer_sa_iterations': (1, 1000000),
    'seed': (0, 2147483647),
    'lambda_coeff': (0.01, 1000),
}


def default_run_options():
    return dict(PROFILE['run_defaults'])


def validate_run_options(options):
    if not isinstance(options, dict) or set(options) - OPTION_LIMITS.keys():
        raise ValueError('Unknown or malformed ALIGN run options')
    for key, value in options.items():
        lower, upper = OPTION_LIMITS[key]
        if key == 'lambda_coeff':
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
                raise ValueError(f'Area / wirelength weight (lambda_coeff) must be a finite number from {lower} to {upper}')
        else:
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f'{key} must be an integer from {lower} to {upper}')
    return deepcopy(options)

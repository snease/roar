"""Validate detached ALIGN overrides without importing Qt or ALIGN."""
from copy import deepcopy


def validate_run_options(options):
    if not isinstance(options, dict) or set(options) - {'nvariants', 'placer_sa_iterations', 'seed', 'lambda_coeff'}:
        raise ValueError('Unknown or malformed ALIGN run options')
    for key, value in options.items():
        if key == 'lambda_coeff':
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.01 <= value <= 1000:
                raise ValueError('Area / wirelength weight (lambda_coeff) must be a finite number from 0.01 to 1000')
        else:
            lower, upper = (0, 2147483647) if key == 'seed' else (1, 100 if key == 'nvariants' else 1000000)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f'{key} must be an integer from {lower} to {upper}')
    return deepcopy(options)

"""Compare each input schema independently against a declared contract."""


def check_schema(contract, profiles):
    if contract is None:
        return {'status': 'not_configured', 'results': []}
    results = []
    for side, profile in profiles.items():
        actual = profile['schema']
        for column, expected in contract['columns'].items():
            observed = actual.get(column)
            issue = ('missing_column' if observed is None else
                     'type_mismatch' if expected is not None and expected != observed else None)
            results.append(dict(side=side, column=column, expected_type=expected,
                                observed_type=observed, status='failed' if issue else 'passed', issue=issue))
        if not contract['allow_extra_columns']:
            for column in sorted(actual.keys() - contract['columns'].keys()):
                results.append(dict(side=side, column=column, expected_type=None,
                                    observed_type=actual[column], status='failed', issue='unexpected_column'))
    return {'status': 'failed' if any(r['status'] == 'failed' for r in results) else 'passed',
            'results': results}

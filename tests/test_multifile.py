import json
import os

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from delta_detective.loading import literal, fingerprint
from delta_detective.validation import validate
from delta_detective.wizard import init_config
from test_features import configure
from test_investigation import setup


def parquet(path, rows, schema='id INTEGER, amount INTEGER'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as con:
        con.execute(f'CREATE TABLE data ({schema})')
        if rows:
            con.executemany('INSERT INTO data VALUES (' + ','.join('?' for _ in rows[0]) + ')', rows)
        con.execute(f'COPY data TO {literal(path)} (FORMAT PARQUET)')
    return path


def test_explicit_lists_align_columns_manifest_and_replay(tmp_path):
    cfg = setup(tmp_path, [], [])
    a = parquet(tmp_path / 'a.parquet', [(1, 10)])
    b = parquet(tmp_path / 'b.parquet', [(20, 2)], 'amount INTEGER, id INTEGER')
    c = parquet(tmp_path / 'c.parquet', [(1, 12), (3, 5)])
    configure(cfg, reference=[a.name, b.name], current=[c.name], dimensions=[])
    out = tmp_path / 'out'
    data = investigate(cfg, out)
    assert data['summary']['delta'] == -13
    assert (data['summary']['added_rows'], data['summary']['removed_rows'], data['summary']['matched_rows']) == (1, 1, 1)
    manifest = json.loads((out / 'manifest.json').read_text())
    profile = manifest['inputs']['reference']
    assert profile['file_count'] == 2 and profile['row_count'] == 2
    assert [f['row_count'] for f in profile['files']] == [1, 1]
    assert [f['sha256'] for f in profile['files']] == [fingerprint(str(a)), fingerprint(str(b))]
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text())
        assert con.execute('SELECT * FROM reference ORDER BY id').fetchall() == [(1, 10), (2, 20)]
        assert con.execute('SELECT sum(row_count) FROM reference_input_files').fetchone() == (2,)


def test_hive_nested_partitions_nulls_encoding_and_root_boundary(tmp_path):
    cfg = setup(tmp_path, [], [])
    for side, amount in [('ref', 10), ('cur', 12)]:
        root = tmp_path / 'not_a_partition=ignored' / side
        parquet(root / 'year=2026' / 'region=East%2FNorth' / 'part.parquet', [(1, amount)])
        parquet(root / 'year=2026' / 'region=__HIVE_DEFAULT_PARTITION__' / 'part.parquet', [(2, 5)])
        (root / '_SUCCESS').write_text('')
    configure(cfg, reference='not_a_partition=ignored/ref', current='not_a_partition=ignored/cur',
              dimensions=['area'], column_mapping={s: {'region': 'area'} for s in ('reference', 'current')},
              filters=[dict(column='year', operator='equals', value='2026')])
    out = tmp_path / 'out'
    data = investigate(cfg, out)
    assert data['summary']['delta'] == 2
    assert {r['category'] for r in data['dimensions'][0]['rows']} == {'East/North', None}
    profile = json.loads((out / 'manifest.json').read_text())['inputs']['reference']
    assert profile['schema']['year'] == profile['schema']['area'] == 'VARCHAR'
    assert 'not_a_partition' not in profile['schema']
    assert profile['input_kind'] == 'directory'
    assert profile['file_count'] == 2
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text())
        assert con.execute('SELECT id, area, year FROM reference ORDER BY id').fetchall() == [(1, 'East/North', '2026'), (2, None, '2026')]
    assert validate(cfg, tmp_path / 'validation')['status'] == 'passed'


def test_replay_freezes_directory_inventory(tmp_path):
    cfg = setup(tmp_path, [], [])
    for side in ('ref', 'cur'):
        parquet(tmp_path / side / 'region=East' / 'one.parquet', [(1, 10)])
    configure(cfg, reference='ref', current='cur', dimensions=['region'])
    out = tmp_path / 'out'
    investigate(cfg, out)
    old = fingerprint(str(tmp_path / 'ref'))
    parquet(tmp_path / 'ref' / 'region=East' / 'new.parquet', [(2, 50)])
    assert fingerprint(str(tmp_path / 'ref')) != old
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text())
        assert con.execute('SELECT count(*) FROM reference').fetchone() == (1,)


def test_duplicate_keys_across_files_diagnosed(tmp_path):
    cfg = setup(tmp_path, [], [])
    parquet(tmp_path / 'one.parquet', [(1, 10)])
    parquet(tmp_path / 'two.parquet', [(1, 20)])
    configure(cfg, reference=['one.parquet', 'two.parquet'], current=['one.parquet'], dimensions=[])
    with pytest.raises(InvestigationError, match='duplicate keys'):
        investigate(cfg, tmp_path / 'out')
    data = validate(cfg, tmp_path / 'validation', export_invalid_rows=True)
    issue = next(c for c in data['checks'] if c['check'] == 'duplicate_key' and c['side'] == 'reference')
    assert issue['details']['affected_rows'] == 2


@pytest.mark.parametrize('schema', ['id BIGINT, amount INTEGER', 'id INTEGER, different INTEGER'])
def test_schema_mismatches_fail_before_union_coercion(tmp_path, schema):
    cfg = setup(tmp_path, [], [])
    parquet(tmp_path / 'one.parquet', [(1, 10)])
    parquet(tmp_path / 'two.parquet', [(2, 20)], schema)
    configure(cfg, reference=['one.parquet', 'two.parquet'], current='one.parquet', dimensions=[])
    with pytest.raises(InvestigationError, match='identical column names and types'):
        investigate(cfg, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('spec', [[], ['reference.parquet', 'reference.parquet'], [['reference.parquet']], ['folder']])
def test_invalid_lists(tmp_path, spec):
    cfg = setup(tmp_path, [], [])
    parquet(tmp_path / 'folder' / 'part.parquet', [(1, 10)])
    configure(cfg, reference=spec)
    with pytest.raises(InvestigationError):
        load_config(cfg)


def test_empty_directory_and_inconsistent_hive_columns(tmp_path):
    cfg = setup(tmp_path, [], [])
    root = tmp_path / 'snapshot'
    root.mkdir()
    configure(cfg, reference='snapshot')
    with pytest.raises(InvestigationError, match='no CSV or Parquet'):
        load_config(cfg)
    parquet(root / 'region=East' / 'one.parquet', [(1, 10)])
    parquet(root / 'two.parquet', [(2, 20)])
    with pytest.raises(InvestigationError, match='same Hive partition columns'):
        load_config(cfg)


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='Named pipes require os.mkfifo')
@pytest.mark.parametrize('suffix', ['csv', 'parquet'])
def test_directory_snapshot_rejects_named_pipes_before_loading(tmp_path, suffix):
    cfg = setup(tmp_path, [], [])
    root = tmp_path / 'snapshot'
    parquet(root / 'valid.parquet', [(1, 10)])
    os.mkfifo(root / f'pipe.{suffix}')
    configure(cfg, reference='snapshot', dimensions=[])
    # Configuration discovery must reject the pipe without opening it, so this
    # regression also fails promptly on a version that accepts nonregular files.
    with pytest.raises(InvestigationError, match='regular files'):
        load_config(cfg)


@pytest.mark.parametrize('stored, succeeds', [('East', True), ('West', False), (None, False)])
def test_physical_partition_columns_are_verified(tmp_path, stored, succeeds):
    cfg = setup(tmp_path, [], [])
    for side in ('ref', 'cur'):
        parquet(tmp_path / side / 'region=East' / 'one.parquet', [(1, 10, stored)],
                'id INTEGER, amount INTEGER, region VARCHAR')
    configure(cfg, reference='ref', current='cur', dimensions=['region'])
    if succeeds:
        assert investigate(cfg, tmp_path / 'out')['summary']['matched_rows'] == 1
    else:
        with pytest.raises(InvestigationError):
            investigate(cfg, tmp_path / 'out')


def test_csv_directory_overrides_and_mixed_formats(tmp_path):
    cfg = setup(tmp_path, [], [])
    for side in ('ref', 'cur'):
        folder = tmp_path / side / 'region=001'
        folder.mkdir(parents=True)
        (folder / 'one.csv').write_text('id,amount\n001,1.20\n')
        parquet(folder / 'two.parquet', [('002', '2.30')], 'id VARCHAR, amount DECIMAL(18,2)')
    configure(cfg, reference='ref', current='cur', dimensions=['region'],
              csv={s: dict(header=True, types={'id': 'VARCHAR', 'amount': 'DECIMAL(18,2)'}) for s in ('reference', 'current')})
    out = tmp_path / 'out'
    data = investigate(cfg, out)
    assert data['summary']['matched_rows'] == 2
    assert data['dimensions'][0]['rows'][0]['category'] == '001'


def test_output_inside_input_is_rejected(tmp_path):
    cfg = setup(tmp_path, [], [])
    parquet(tmp_path / 'ref' / 'one.parquet', [(1, 10)])
    configure(cfg, reference='ref', current='ref', dimensions=[])
    for operation in (investigate, validate):
        with pytest.raises(InvestigationError, match='outside input'):
            operation(cfg, tmp_path / 'ref' / 'out')
    with pytest.raises(InvestigationError, match='outside input'):
        init_config(tmp_path / 'ref', tmp_path / 'ref', tmp_path / 'ref' / 'config.yaml')


def test_wizard_accepts_directory_snapshots(tmp_path):
    for side in ('ref', 'cur'):
        parquet(tmp_path / side / 'region=East' / 'one.parquet', [(1, 10)])
        parquet(tmp_path / side / 'region=West' / 'two.parquet', [(2, 20)])
    answers = iter(['', '1', '2', '', '2', '', '', ''])
    path = init_config(tmp_path / 'ref', tmp_path / 'cur', tmp_path / 'new.yaml',
                       ask=lambda p: next(answers), tell=lambda s: None)
    assert list(answers) == []
    assert load_config(path)['reference'] == str((tmp_path / 'ref').resolve())
    assert investigate(path, tmp_path / 'out')['summary']['matched_rows'] == 2


def test_directory_membership_change_aborts_without_publication(tmp_path, monkeypatch):
    cfg = setup(tmp_path, [], [])
    for side in ('ref', 'cur'):
        parquet(tmp_path / side / 'one.parquet', [(1, 10)])
    configure(cfg, reference='ref', current='cur', dimensions=[])
    import delta_detective.core as core
    original = core.load_snapshots
    def changed(*args):
        profiles = original(*args)
        parquet(tmp_path / 'ref' / 'new.parquet', [(2, 20)])
        return profiles
    monkeypatch.setattr(core, 'load_snapshots', changed)
    with pytest.raises(InvestigationError, match='Input changed'):
        investigate(cfg, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()

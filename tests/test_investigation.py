import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
import duckdb
import pytest
import yaml
from delta_detective import investigate
from delta_detective.config import InvestigationError
from delta_detective.demo import demo
from delta_detective.loading import literal


def setup(tmp_path, ref_rows, cur_rows, schema='id INTEGER, amount DECIMAL(18,2), category VARCHAR', **overrides):
    with duckdb.connect() as con:
        for side, rows in [('reference', ref_rows), ('current', cur_rows)]:
            con.execute(f'CREATE TABLE {side} ({schema})')
            if rows:
                con.executemany(f'INSERT INTO {side} VALUES ({",".join("?" for _ in rows[0])})', rows)
            con.execute(f'COPY {side} TO {literal(tmp_path / (side+".parquet"))} (FORMAT PARQUET)')
    cfg = dict(mode='snapshots', reference='reference.parquet', current='current.parquet', key=['id'],
               metric=dict(name='amount', aggregate='sum', column='amount', null_policy='error'), dimensions=['category'])
    cfg.update(overrides)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
    return path


def assert_valid(data, totals, contributions):
    s = data['summary']
    assert (s['reference_total'], s['current_total']) == totals
    assert tuple(s[k+'_contribution'] for k in ['added', 'removed', 'matched']) == contributions
    assert s['reference_rows'] == s['removed_rows'] + s['matched_rows']
    assert s['current_rows'] == s['added_rows'] + s['matched_rows']
    assert s['status'] == 'passed'
    assert abs(s['delta'] - sum(contributions)) <= s['tolerance']
    for d in data['dimensions']:
        assert abs(sum(r['contribution'] for r in d['rows']) - s['delta']) <= d['check']['tolerance']


def test_demo_and_replay(tmp_path):
    cfg = demo(tmp_path / 'demo')
    out = tmp_path / 'out'
    data = investigate(cfg, out)
    assert_valid(data, (100000, 82000), (2000, -14000, -6000))
    assert data['reclassifications'][0]['rows'] == 1
    assert data['reclassifications'][0]['also_metric_changed'] == 0
    assert not (out / 'raw_rows.csv').exists()
    assert set(p.name for p in out.iterdir()) == {'report.html','findings.json','analysis.sql','manifest.json'}
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT reference_total,current_total,added_contribution,removed_contribution,matched_contribution FROM reconciliation').fetchone() == (100000,82000,2000,-14000,-6000)
    assert json.loads((out / 'findings.json').read_text())['summary']['delta'] == '-18000.00'
    assert '-18000.00' in (out / 'report.html').read_text()


@pytest.mark.parametrize('ref,cur,totals,parts', [
    ([(1,10,'a')],[(1,10,'a')],(10,10),(0,0,0)),
    ([(1,10,'a'),(2,20,'b')],[(2,20,'b'),(1,10,'a')],(30,30),(0,0,0)),
    ([(1,10,'a')],[(1,10,'a'),(2,5,'b')],(10,15),(5,0,0)),
    ([(1,10,'a'),(2,5,'b')],[(1,10,'a')],(15,10),(0,-5,0)),
    ([(1,10,'a')],[(1,7,'a')],(10,7),(0,0,-3)),
    ([],[],(0,0),(0,0,0)),
    ([],[(1,5,'a')],(0,5),(5,0,0)),
    ([(1,5,'a')],[],(5,0),(0,-5,0)),
    ([(1,0,'a')],[(1,-2,'a')],(0,-2),(0,0,-2)),
    ([(1,-10,'a')],[(1,-7,'a'),(2,-2,'b')],(-10,-9),(-2,0,3)),
    ([(1,'0.10','a')],[(1,'0.30','a')],(Decimal('.1'),Decimal('.3')),(0,0,Decimal('.2'))),
])
def test_cases(tmp_path, ref, cur, totals, parts):
    result = investigate(setup(tmp_path, ref, cur), tmp_path/'out')
    assert_valid(result, totals, parts)
    if not totals[0]:
        assert result['summary']['percent_change'] is None


def test_composite_and_count(tmp_path):
    cfg = setup(tmp_path, [(1,'ab',10,'a'),(1,'bc',20,'b')], [(1,'bc',99,'c'),(2,'ab',30,'a')],
                schema='id INTEGER, sub VARCHAR, amount INTEGER, category VARCHAR', key=['id','sub'],
                metric={'name':'rows','aggregate':'count'})
    data = investigate(cfg,tmp_path/'out')
    assert_valid(data,(2,2),(1,-1,0))
    assert data['summary']['metric_changed_rows'] == 0


@pytest.mark.parametrize('side', ['reference','current'])
@pytest.mark.parametrize('bad,match', [([(1,1,'a'),(1,2,'b')],'duplicate'), ([(None,1,'a')],'null key')])
def test_bad_keys(tmp_path, side, bad, match):
    cfg = setup(tmp_path, bad if side=='reference' else [(1,1,'a')],bad if side=='current' else [(1,1,'a')])
    with pytest.raises(InvestigationError, match=match):
        investigate(cfg,tmp_path/'out')
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('value', [None,float('nan'),float('inf'),-float('inf')])
def test_bad_metric(tmp_path, value):
    cfg = setup(tmp_path,[(1,1,'a')],[(1,value,'a')],schema='id INTEGER, amount DOUBLE, category VARCHAR')
    with pytest.raises(InvestigationError, match='null or nonfinite'):
        investigate(cfg,tmp_path/'out')


def test_float(tmp_path):
    cfg = setup(tmp_path,[(1,.1,'a'),(2,.2,'a')],[(1,.3,'a'),(2,.2,'b')],schema='id INTEGER, amount DOUBLE, category VARCHAR')
    data = investigate(cfg,tmp_path/'out')
    assert data['summary']['exact'] is False
    assert data['summary']['delta'] == pytest.approx(.2)
    assert_valid(data,(.30000000000000004,.5),(0,0,.3-.1))


def test_categories_and_escaping(tmp_path):
    cfg = setup(tmp_path,[(1,10,None),(2,20,'(NULL)'),(3,5,'gone')],[(1,10,'<script>alert(1)</script>'),(2,20,'(NULL)'),(4,7,'new')])
    data = investigate(cfg,tmp_path/'out')
    assert_valid(data,(35,37),(7,-5,0))
    groups = {r['category']:r for r in data['dimensions'][0]['rows']}
    assert groups[None]['contribution'] == -10
    assert groups['(NULL)']['contribution'] == 0
    html = (tmp_path/'out/report.html').read_text()
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert data['summary']['dimension_changed_rows'] == 1


def test_unusual_names(tmp_path):
    cfg = setup(tmp_path,[(1,1,'a')],[(1,2,'b')],schema='"a""key" INTEGER, "sum; drop" DECIMAL(18,2), "<dim>" VARCHAR',
                key=['a"key'],metric=dict(name='test',aggregate='sum',column='sum; drop',null_policy='error'), dimensions=['<dim>'])
    assert_valid(investigate(cfg,tmp_path/'out'),(1,2),(0,0,1))


def test_missing_and_incompatible(tmp_path):
    cfg = setup(tmp_path,[(1,1,'a')],[(1,2,'b')])
    settings = yaml.safe_load(cfg.read_text())
    settings['key'] = ['missing']
    cfg.write_text(yaml.safe_dump(settings))
    with pytest.raises(InvestigationError,match='missing required'):
        investigate(cfg,tmp_path/'out')
    settings['key'] = ['id']
    cfg.write_text(yaml.safe_dump(settings))
    with duckdb.connect() as con:
        con.execute(f"COPY (SELECT '1'::VARCHAR AS id, 2::DECIMAL(18,2) AS amount, 'b' AS category) TO {literal(tmp_path/'current.parquet')} (FORMAT PARQUET)")
    with pytest.raises(InvestigationError,match='Incompatible'):
        investigate(cfg,tmp_path/'out')


@pytest.mark.parametrize('change', [dict(mode='periods'),dict(unknown=True),dict(key=[]),dict(report={'include_raw_rows':'false'}),dict(metric={'name':'x','aggregate':'count','column':'x'}),dict(metric={'name':'x','aggregate':'sum','column':'amount','null_policy':'zero'}),dict(reference='https://example.com/x.csv')])
def test_config(tmp_path, change):
    cfg = setup(tmp_path,[],[],**change)
    with pytest.raises(InvestigationError):
        investigate(cfg,tmp_path/'out')


def test_raw_and_overwrite(tmp_path):
    cfg = setup(tmp_path,[(987654321,1,'a')],[(987654321,2,'b')])
    out = tmp_path/'out'
    investigate(cfg,out)
    for p in out.iterdir():
        assert '987654321' not in p.read_text(encoding='utf-8')
    with pytest.raises(InvestigationError,match='nonempty'):
        investigate(cfg,out)
    settings = yaml.safe_load(cfg.read_text())
    settings['report'] = {'include_raw_rows':True}
    cfg.write_text(yaml.safe_dump(settings))
    investigate(cfg,out,overwrite=True)
    assert '987654321' in (out/'raw_rows.csv').read_text()
    with pytest.raises(InvestigationError,match='contain'):
        investigate(cfg,tmp_path,overwrite=True)


def test_other(tmp_path):
    cfg = setup(tmp_path,[],[(i,i+1,str(i)) for i in range(60)])
    data = investigate(cfg,tmp_path/'out')
    assert_valid(data,(0,1830),(1830,0,0))
    rows = data['dimensions'][0]['rows']
    assert len(rows) == 51
    assert rows[-1]['is_other']
    assert rows[-1]['contribution'] == 55


def test_csv_and_replay(tmp_path):
    cfg = setup(tmp_path,[],[])
    settings = yaml.safe_load(cfg.read_text())
    for side, amount in [('reference',10),('current',8)]:
        path = tmp_path/(side+'.csv')
        path.write_text(f'id,amount,category\n1,{amount},a\n')
        settings[side] = path.name
    cfg.write_text(yaml.safe_dump(settings))
    assert_valid(investigate(cfg,tmp_path/'out'),(10,8),(0,0,-2))
    with duckdb.connect() as con:
        con.execute((tmp_path/'out/analysis.sql').read_text())
        assert con.execute('SELECT current_total-reference_total FROM reconciliation').fetchone()[0] == -2
    manifest = json.loads((tmp_path/'out/manifest.json').read_text())
    assert manifest['inputs']['reference']['parsing']['strict_mode']
    (tmp_path/'current.csv').write_text('id,amount,category\n1,broken,a\n')
    with pytest.raises(InvestigationError,match='numeric'):
        investigate(cfg,tmp_path/'bad')


def test_overflow(tmp_path):
    high = '9'*38
    cfg = setup(tmp_path,[(1,high,'a'),(2,high,'a')],[],schema='id INTEGER, amount DECIMAL(38,0), category VARCHAR')
    with pytest.raises(InvestigationError, match='overflow'):
        investigate(cfg,tmp_path/'out')


def test_cli(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable,'-m','delta_detective.cli',*map(str,args)],capture_output=True,text=True)
    assert run('demo','--out',tmp_path/'demo').returncode == 0
    result = run('investigate',tmp_path/'demo/comparison.yaml','--out',tmp_path/'out')
    assert result.returncode == 0, result.stderr
    assert '-18000.00' in result.stdout
    assert (tmp_path/'out/report.html').is_file()
    assert run('investigate',tmp_path/'demo/comparison.yaml','--out',tmp_path/'out').returncode != 0


def test_unrelated_schema_changes(tmp_path):
    cfg = setup(tmp_path,[(1,1,'a')],[(1,1,'a')])
    with duckdb.connect() as con:
        con.execute(f"COPY (SELECT 1 AS id, 1::DECIMAL(18,2) AS amount, 'a' AS category, 'new' AS extra) TO {literal(tmp_path/'current.parquet')} (FORMAT PARQUET)")
    data = investigate(cfg,tmp_path/'out')
    assert_valid(data,(1,1),(0,0,0))
    assert data['schema_changes'] == [{'column':'extra','reference_type':None,'current_type':'VARCHAR'}]


@pytest.mark.parametrize('text', ['mode: snapshots\nmode: snapshots', '!!python/object:bad {}', '[not, a, mapping]', 'metric: ['])
def test_malformed_yaml(tmp_path,text):
    cfg=tmp_path/'config.yaml'
    cfg.write_text(text)
    with pytest.raises(InvestigationError):
        investigate(cfg,tmp_path/'out')


def test_malformed_csv(tmp_path):
    cfg=setup(tmp_path,[],[])
    settings=yaml.safe_load(cfg.read_text())
    settings['current']='bad.csv'
    cfg.write_text(yaml.safe_dump(settings))
    (tmp_path/'bad.csv').write_text('id,amount,category\n1,2,a\n2,3,b,unexpected\n')
    with pytest.raises(InvestigationError):
        investigate(cfg,tmp_path/'out')


def test_failed_residual():
    from delta_detective.comparison import check
    with pytest.raises(InvestigationError,match='Reconciliation failed'):
        check(100,90,[-9],False)


def test_high_precision_and_count_null(tmp_path):
    cfg=setup(tmp_path,[(1,'99999999999999999999999999999.01','a')],[(1,'99999999999999999999999999999.02','a')],schema='id INTEGER, amount DECIMAL(38,2), category VARCHAR')
    data=investigate(cfg,tmp_path/'out')
    assert data['summary']['delta']==Decimal('.01')
    assert data['summary']['residual']==0
    settings=yaml.safe_load(cfg.read_text())
    settings['metric']={'name':'rows','aggregate':'count'}
    cfg.write_text(yaml.safe_dump(settings))
    assert_valid(investigate(cfg,tmp_path/'count'),(1,1),(0,0,0))


def test_count_star_includes_null_amounts(tmp_path):
    cfg=setup(tmp_path,[(1,None,'a')],[(1,None,'a'),(2,None,'b')],metric={'name':'rows','aggregate':'count'})
    assert_valid(investigate(cfg,tmp_path/'out'),(1,2),(1,0,0))


def test_standalone_html(tmp_path):
    from html.parser import HTMLParser
    class Inspector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag not in ('script','iframe','link','img','object','embed')
            assert not any(k.startswith('on') or k == 'src' for k,v in attrs)
            assert all(v.startswith('#') for k,v in attrs if k == 'href')
    cfg=demo(tmp_path/'demo')
    investigate(cfg,tmp_path/'out')
    html=(tmp_path/'out/report.html').read_text(encoding='utf-8')
    Inspector().feed(html)
    assert 'url(' not in html
    for title in ['Metric reconciliation','Keys and selected-field differences','Independent dimension breakdowns','Selected-dimension reclassifications','Validation and schema','Executed SQL']:
        assert title in html


@pytest.mark.parametrize('category_type,value', [
    ('INTERVAL', '1 day'),
    ('INTEGER[]', [1, 2]),
    ('STRUCT(code INTEGER)', {'code': 1}),
    ('DECIMAL(10,2)[]', [Decimal('1.25')]),
])
def test_unsupported_dimensions(tmp_path, category_type, value):
    cfg = setup(tmp_path, [(1, 1, value)], [(1, 2, value)],
                schema=f'id INTEGER, amount INTEGER, category {category_type}')
    with pytest.raises(InvestigationError, match='must be categorical'):
        investigate(cfg, tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_dimension_cancellation_tolerance(tmp_path):
    from delta_detective.config import load_config
    from delta_detective.loading import load_and_validate
    from delta_detective.comparison import compare
    cfg = load_config(setup(tmp_path, [], [(1, 1e16, 'a0'), (2, -1e16, 'b0'), (3, 1, 'a0')],
                            schema='id INTEGER, amount DOUBLE, category VARCHAR'))
    with duckdb.connect(config={'threads': 1}) as con:
        profiles = load_and_validate(con, cfg, con.execute)
        summary, dimensions, _ = compare(con, cfg, profiles, con.execute)
    assert summary['current_total'] == 1
    assert dimensions[0]['check']['residual'] == 1
    assert dimensions[0]['check']['tolerance'] == 20000
    assert dimensions[0]['check']['status'] == 'passed'


@pytest.mark.parametrize('failure_point', ['backup', 'publish'])
def test_failed_publication_preserves_previous_bundle(tmp_path, monkeypatch, failure_point):
    cfg = demo(tmp_path/'demo')
    out = tmp_path/'out'
    investigate(cfg, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    original_replace = Path.replace

    def fail_replace(self, target):
        if ((failure_point == 'backup' and self == out)
                or (failure_point == 'publish' and self.name.startswith('.delta-')
                    and not self.name.startswith('.delta-backup-'))):
            raise PermissionError('simulated publication failure')
        return original_replace(self, target)

    monkeypatch.setattr(Path, 'replace', fail_replace)
    with pytest.raises(PermissionError, match='simulated publication failure'):
        investigate(cfg, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert not list(tmp_path.glob('.delta-*'))


def test_demo_overwrite_replaces_bundle(tmp_path):
    out = tmp_path/'demo'
    demo(out)
    (out/'raw_rows.csv').write_text('old evidence')
    demo(out, overwrite=True)
    assert {p.name for p in out.iterdir()} == {'reference.parquet', 'current.parquet', 'comparison.yaml'}
    assert_valid(investigate(out/'comparison.yaml', tmp_path/'out'),
                 (100000, 82000), (2000, -14000, -6000))


def test_demo_failure_preserves_bundle(tmp_path, monkeypatch):
    import importlib
    demo_module = importlib.import_module('delta_detective.demo')
    out = tmp_path/'demo'
    demo(out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}

    def fail_write(stage):
        (stage/'reference.parquet').write_bytes(b'partial input')
        raise duckdb.IOException('simulated disk failure')

    monkeypatch.setattr(demo_module, '_write_demo', fail_write)
    with pytest.raises(InvestigationError, match='Could not generate demo'):
        demo(out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert not list(tmp_path.glob('.delta-*'))


@pytest.mark.parametrize('command', ['investigate', 'schema', 'validate', 'demo'])
@pytest.mark.parametrize('initially_empty', [False, True])
def test_publication_preserves_concurrent_output(tmp_path, monkeypatch, command, initially_empty):
    import importlib
    cfg = setup(tmp_path, [(1, 1, 'a')], [(1, 2, 'b')])
    if command == 'schema':
        settings = yaml.safe_load(cfg.read_text())
        settings['schema'] = {'columns': {'missing': None}}
        cfg.write_text(yaml.safe_dump(settings))
    module_name = {'investigate': 'core', 'schema': 'core', 'validate': 'validation', 'demo': 'demo'}[command]
    module = importlib.import_module('delta_detective.' + module_name)
    original_publish = module.publish
    out = tmp_path / 'out'
    if initially_empty:
        out.mkdir()

    def concurrent_publish(stage, target, *args, **kwargs):
        target.mkdir(exist_ok=True)
        (target / 'concurrent.txt').write_text('Keep the other writer\'s output')
        return original_publish(stage, target, *args, **kwargs)

    monkeypatch.setattr(module, 'publish', concurrent_publish)
    with pytest.raises(OSError):
        if command == 'demo':
            module.demo(out)
        elif command == 'validate':
            module.validate(cfg, out)
        else:
            module.investigate(cfg, out)
    assert {p.name: p.read_text() for p in out.iterdir()} == {
        'concurrent.txt': 'Keep the other writer\'s output'}
    assert not list(tmp_path.glob('.delta-*'))


def test_publication_accepts_empty_directory_without_directory_replacement(tmp_path, monkeypatch):
    from delta_detective.core import publish

    stage, out = tmp_path / 'stage', tmp_path / 'out'
    stage.mkdir()
    out.mkdir()
    (stage / 'report.txt').write_text('Completed report')
    original_replace = Path.replace

    def replace_without_existing_directory(self, target):
        if target.exists():
            raise FileExistsError('Platform cannot replace an existing directory')
        return original_replace(self, target)

    monkeypatch.setattr(Path, 'replace', replace_without_existing_directory)
    publish(stage, out)
    assert (out / 'report.txt').read_text() == 'Completed report'
    assert not stage.exists()


@pytest.mark.parametrize('suffix', ['csv', 'parquet'])
@pytest.mark.parametrize('brackets_in_parent', [False, True])
def test_glob_input_paths_rejected(tmp_path, suffix, brackets_in_parent):
    cfg = setup(tmp_path, [], [])
    folder = tmp_path/'input[1]' if brackets_in_parent else tmp_path
    folder.mkdir(exist_ok=True)
    path = folder/f'{"data" if brackets_in_parent else "data[1]"}.{suffix}'
    path.write_bytes((tmp_path/'reference.parquet').read_bytes())
    settings = yaml.safe_load(cfg.read_text())
    settings['reference'] = str(path)
    cfg.write_text(yaml.safe_dump(settings))
    with pytest.raises(InvestigationError, match='glob characters'):
        investigate(cfg, tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_float_tolerance_overflow():
    from delta_detective.comparison import check
    with pytest.raises(InvestigationError, match='numeric overflow'):
        check(0., 0., [1e308, -1e308], True)


def test_float_aggregate_overflow_cli(tmp_path):
    cfg = setup(tmp_path, [], [(1, 1e308, 'a'), (2, 1e308, 'a')],
                schema='id INTEGER, amount DOUBLE, category VARCHAR')
    result = subprocess.run([sys.executable, '-m', 'delta_detective.cli', 'investigate',
                             str(cfg), '--out', str(tmp_path/'out')], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'numeric overflow' in result.stderr
    assert 'Traceback' not in result.stderr
    assert not (tmp_path/'out').exists()


def test_nonfinite_evidence_rejected():
    from delta_detective.findings import dumps
    with pytest.raises(InvestigationError, match='nonfinite result'):
        dumps({'amount': float('inf')})


def test_non_utf8_config_cli(tmp_path):
    cfg = tmp_path/'config.yaml'
    cfg.write_text('mode: snapshots', encoding='utf-16')
    result = subprocess.run([sys.executable, '-m', 'delta_detective.cli', 'investigate',
                             str(cfg), '--out', str(tmp_path/'out')], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'Cannot read configuration' in result.stderr
    assert 'Traceback' not in result.stderr
    assert not (tmp_path/'out').exists()


def test_nul_input_path_cli(tmp_path):
    cfg = setup(tmp_path, [], [], reference='bad\x00.csv')
    result = subprocess.run([sys.executable, '-m', 'delta_detective.cli', 'investigate',
                             str(cfg), '--out', str(tmp_path/'out')], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'input paths must not contain NUL' in result.stderr
    assert 'Traceback' not in result.stderr
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('suffix', ['csv', 'parquet'])
@pytest.mark.parametrize('partition_column', ['id', 'amount', 'category'])
def test_parent_directories_do_not_override_input_values(tmp_path, suffix, partition_column):
    cfg = setup(tmp_path, [(1, 10, 'a')], [(1, 8, 'b')])
    settings = yaml.safe_load(cfg.read_text())
    for side, partition, amount, category in [('reference', 999, 10, 'a'), ('current', 888, 8, 'b')]:
        folder = tmp_path/f'{partition_column}={partition}'
        folder.mkdir()
        path = folder/f'{side}.{suffix}'
        if suffix == 'csv':
            path.write_text(f'id,amount,category\n1,{amount},{category}\n')
        else:
            path.write_bytes((tmp_path/f'{side}.parquet').read_bytes())
        settings[side] = str(path)
    cfg.write_text(yaml.safe_dump(settings))
    out = tmp_path/'out'
    data = investigate(cfg, out)
    assert_valid(data, (10, 8), (0, 0, -2))
    assert {row['category'] for row in data['dimensions'][0]['rows']} == {'a', 'b'}
    manifest = json.loads((out/'manifest.json').read_text())
    assert all(profile['parsing']['hive_partitioning'] is False for profile in manifest['inputs'].values())
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text())
        assert con.execute('SELECT reference_total,current_total,matched_rows FROM reconciliation').fetchone() == (10, 8, 1)

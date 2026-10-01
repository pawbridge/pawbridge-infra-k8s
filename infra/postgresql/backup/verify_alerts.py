"""실제 백업 경보식의 성공·실패·재시도·중지·지표 누락을 합성 시계열로 검증한다."""
import argparse
from pathlib import Path
import subprocess
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[3]
RULES = yaml.safe_load((ROOT / 'gitops/stateful/postgresql-backup/rules.yaml').read_text())['spec']
ALERTS = {r['alert']: r for group in RULES['groups'] for r in group['rules'] if 'alert' in r}
CJ = 'namespace="databases",cronjob="pawbridge-postgresql-backup"'


def sample(name, labels, values):
    return {'series': name + '{' + labels + '}', 'values': values}


def baseline(suspend=0, created=0, last=None):
    values = [sample('kube_cronjob_info', CJ, '1+0x1600'),
              sample('kube_cronjob_spec_suspend', CJ, str(suspend)+'+0x1600'),
              sample('kube_cronjob_created', CJ, str(created)+'+0x1600')]
    if last is not None:
        values.append(sample('kube_cronjob_status_last_successful_time', CJ, str(last)+'+0x1600'))
    for kind in ['CronJob', 'Job']:
        values.append(sample('kube_state_metrics_list_total', 'resource="*v1.'+kind+'",result="success"', '1+0x1600'))
    return values


def job(name, created, failed, owner='pawbridge-postgresql-backup'):
    labels = 'namespace="databases",job_name="'+name+'"'
    return [sample('kube_job_created', labels, str(created)+'+0x1600'),
            sample('kube_job_owner', labels+',owner_kind="CronJob",owner_name="'+owner+'",owner_is_controller="true"', '1+0x1600'),
            sample('kube_job_failed', labels+',condition="true"', str(failed)+'+0x1600')]


def test_case(series, time, fired=()):
    alert_tests = []
    for name, rule in ALERTS.items():
        expected = []
        if name in fired:
            labels = dict(rule['labels'])
            if name != 'PawBridgePostgreSQLBackupCollectionUnhealthy':
                labels.update(namespace='databases', cronjob='pawbridge-postgresql-backup')
            expected.append({'exp_labels': labels, 'exp_annotations': rule['annotations']})
        alert_tests.append({'eval_time': time, 'alertname': name, 'exp_alerts': expected})
    return {'interval': '1m', 'input_series': series, 'alert_rule_test': alert_tests}


def verify(promtool):
    cases = [
        test_case(baseline(last=1)+job('old',1,1)+job('new',2,0), '20m'),
        test_case(baseline(last=1)+job('old',1,0)+job('new',2,1), '20m', ['PawBridgePostgreSQLBackupFailed']),
        test_case(baseline(last=1)+job('retry',2,0)+[sample('kube_job_status_failed','namespace="databases",job_name="retry",reason="BackoffLimitExceeded"','1+0x1600')], '20m'),
        test_case(baseline(last=1)+job('foreign',2,1,owner='unrelated-job'), '20m'),
        test_case(baseline(), '14h'),
        test_case(baseline(), '15h10m', ['PawBridgePostgreSQLBackupStale']),
        test_case(baseline(last=0), '15h10m', ['PawBridgePostgreSQLBackupStale']),
        test_case(baseline(last=1), '16h', ['PawBridgePostgreSQLBackupStale']),
        test_case(baseline(last=12*3600), '16h'),
        test_case(baseline(suspend=1)+job('failed',1,1), '16h'),
        test_case(baseline(suspend=1), '25h', ['PawBridgePostgreSQLBackupSuspended']),
        test_case([s for s in baseline() if not s['series'].startswith('kube_cronjob_info')], '10m', ['PawBridgePostgreSQLBackupMissing']),
        test_case([s for s in baseline() if '*v1.Job' not in s['series']], '10m', ['PawBridgePostgreSQLBackupCollectionUnhealthy']),
        test_case(baseline()+[sample('kube_state_metrics_watch_total','resource="*v1.Job",result="error"','0+1x1600')], '15m', ['PawBridgePostgreSQLBackupCollectionUnhealthy']),
    ]
    with tempfile.TemporaryDirectory(prefix='backup-rules-') as work:
        work = Path(work)
        (work/'rules.yaml').write_text(yaml.safe_dump(RULES, allow_unicode=True))
        (work/'tests.yaml').write_text(yaml.safe_dump({'rule_files':['rules.yaml'], 'evaluation_interval':'1m', 'tests':cases}, allow_unicode=True))
        subprocess.run([promtool,'check','rules',str(work/'rules.yaml')],check=True,timeout=30)
        subprocess.run([promtool,'test','rules',str(work/'tests.yaml')],check=True,timeout=90,cwd=work)
    print('Backup alert scenarios passed:',len(cases))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('promtool')
    verify(parser.parse_args().promtool)

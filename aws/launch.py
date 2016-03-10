import aws.ec2
import pager
import re
import os
import datetime
import time
import random
import json
import argh
import logging
import pool.thread
import shell
import shell.conf
import sys
import traceback
import uuid
import util.colors
import util.iter
import util.log
from unittest import mock


is_cli = False


def _tagify(old):
    new = (old
           .replace(',', '-')
           .replace(' ', '-')
           .replace('_', '-')
           .replace('/', '-'))
    if new != old:
        logging.info("tagified label: '%s' -> '%s'", old, new)
    return new


def _cmd(arg, cmd, no_rm, bucket):
    _cmd = cmd % {'arg': arg}
    kw = {'bucket': bucket,
          'user': shell.run('whoami'),
          'date': shell.run('date -u +%Y-%m-%dT%H:%M:%SZ'),
          'tags': '$(aws ec2 describe-tags --filters "Name=resource-id,Values=$(curl http://169.254.169.254/latest/meta-data/instance-id/ 2>/dev/null)"|python3 -c \'import sys, json; print(",".join(["%(Key)s=%(Value)s".replace(",", "-") % x for x in json.load(sys.stdin)["Tags"]]).replace("/", "-").replace(" ", "-").replace("_", "-"))\')'} # noqa
    path = 's3://%(bucket)s/ec2_logs/%(user)s/%(date)s_%(tags)s' % kw
    upload_log = 'aws s3 cp ~/nohup.out %(path)s/nohup.out >/dev/null 2>&1' % locals()
    upload_log_tail = 'tail -n 1000 ~/nohup.out > ~/nohup.out.tail; aws s3 cp ~/nohup.out.tail %(path)s/nohup.out.tail >/dev/null 2>&1' % locals()
    shutdown = ('sudo halt'
                if no_rm else
                'aws ec2 terminate-instances --instance-ids $(curl http://169.254.169.254/latest/meta-data/instance-id/ 2>/dev/null)')
    return "(echo %(_cmd)s; %(_cmd)s; echo exited $?; %(upload_log)s; %(upload_log_tail)s; %(shutdown)s) >nohup.out 2>nohup.out </dev/null &" % locals()


@argh.arg('--tag', action='append')
@argh.arg('--arg', action='append')
@argh.arg('--label', action='append')
def new(name:    'name of all instances',
        arg:     'one instance per arg, and that arg is str formatted into cmd, pre_cmd, and tags as "arg"' = None,
        label:   'one label per arg, to use as ec2 tag since arg is often inapproriate, defaults to arg if not provided' = None,
        pre_cmd: 'optional cmd which runs before cmd is backgrounded' = None,
        cmd:     'cmd which is run in the background' = None,
        tag:     'tag to set as "<key>=<value>' = None,
        no_rm:   'stop instance instead of terminating when done' = False,
        bucket:  's3 bucket to upload logs to' = shell.conf.get_or_prompt_pref('ec2_logs_bucket',  __file__, message='bucket for ec2_logs'),
        # following opts are copied verbatim from ec2.new
        spot:    'spot price to bid'           = None,
        key:     'key pair name'               = shell.conf.get_or_prompt_pref('key',  aws.ec2.__file__, message='key pair name'),
        ami:     'ami id'                      = shell.conf.get_or_prompt_pref('ami',  aws.ec2.__file__, message='ami id'),
        sg:      'security group name'         = shell.conf.get_or_prompt_pref('sg',   aws.ec2.__file__, message='security group name'),
        type:    'instance type'               = shell.conf.get_or_prompt_pref('type', aws.ec2.__file__, message='instance type'),
        vpc:     'vpc name'                    = shell.conf.get_or_prompt_pref('vpc',  aws.ec2.__file__, message='vpc name'),
        zone:    'ec2 availability zone'       = None,
        gigs:    'gb capacity of primary disk' = 8):
    tags, args, labels = tuple(tag or ()), tuple(arg or ()), tuple(label or ())
    args = [str(a) for a in args]
    if labels:
        assert len(args) == len(labels), 'there must be an equal number of args and labels, %s != %s' % (len(args), len(labels))
    else:
        labels = args
    labels = [_tagify(x) for x in labels]
    for tag in tags:
        assert '=' in tag, 'tags should be "<key>=<value>", not: %s' % tag
    for label, arg in zip(labels, args):
        if label == arg:
            logging.info('going to launch arg: %s', arg)
        else:
            logging.info('going to launch label: %s, arg: %s', label, arg)
    if pre_cmd and os.path.exists(pre_cmd):
        logging.info('reading pre_cmd from file: %s', os.path.abspath(pre_cmd))
        with open(pre_cmd) as f:
            pre_cmd = f.read()
    if os.path.exists(cmd):
        logging.info('reading cmd from file: %s', os.path.abspath(cmd))
        with open(cmd) as f:
            cmd = f.read()
    launch_id = str(uuid.uuid4())
    logging.info('launch=%s', launch_id)
    data = json.dumps({'name': name,
                       'args': args,
                       'labels': labels,
                       'pre_cmd': pre_cmd,
                       'cmd': cmd,
                       'tags': tags,
                       'no_rm': no_rm,
                       'bucket': bucket,
                       'spot': spot,
                       'key': key,
                       'ami': ami,
                       'sg': sg,
                       'type': type,
                       'vpc': vpc,
                       'gigs': gigs})
    if 'AWS_LAUNCH_RUN_LOCAL' in os.environ:
        for arg in args:
            with shell.tempdir(), shell.set_stream():
                shell.run(pre_cmd % {'arg': arg})
                shell.run(cmd % {'arg': arg})
    else:
        user = shell.run('whoami')
        shell.run('aws s3 cp - s3://%(bucket)s/ec2_logs/%(user)s/launch=%(launch_id)s.json' % locals(), stdin=data)
        instance_ids = aws.ec2.new(name,
                                   spot=spot,
                                   key=key,
                                   ami=ami,
                                   sg=sg,
                                   type=type,
                                   vpc=vpc,
                                   zone=zone,
                                   gigs=gigs,
                                   num=len(args))
        errors = []
        tags += ('launch=%s' % launch_id,)
        def run_cmd(instance_id, arg, label):
            def fn():
                try:
                    if pre_cmd:
                        aws.ec2.ssh(instance_id, yes=True, cmd=pre_cmd % {'arg': arg}, prefixed=True)
                    aws.ec2.ssh(instance_id, no_tty=True, yes=True, cmd=_cmd(arg, cmd, no_rm, bucket), prefixed=True)
                    instance = aws.ec2._ls([instance_id])[0]
                    aws.ec2._retry(instance.create_tags)(Tags=[{'Key': k, 'Value': v}
                                                               for tag in tags + ('label=%s' % label,)
                                                               for [k, v] in [tag.split('=', 1)]])
                    logging.info('tagged: %s', aws.ec2._pretty(instance))
                    logging.info('ran cmd against %s for label %s', instance_id, label)
                except:
                    errors.append(traceback.format_exc())
            return fn
        pool.thread.wait(*map(run_cmd, instance_ids, args, labels))
        try:
            if errors:
                logging.info(util.colors.red('errors:'))
                for e in errors:
                    logging.info(e)
                sys.exit(1)
        finally:
            return 'launch=%s' % launch_id


def wait(*tags):
    """
    wait for all args to finish, and exit 0 only if all logged "exited 0".
    """
    if 'AWS_LAUNCH_RUN_LOCAL' not in os.environ:
        logging.info('wait for launch: %s', ' '.join(tags))
        while True:
            instances = aws.ec2._ls(tags, state=['running', 'pending'])
            logging.info('%s num running: %s', str(datetime.datetime.utcnow()).replace(' ', 'T').split('.')[0], len(instances))
            if not instances:
                break
            time.sleep(5 + 10 * random.random())
        vals = status(*tags)
        logging.info('\n'.join(vals))
        for v in vals:
            if not v.startswith('done'):
                sys.exit(1)


def from_params(params_path):
    with open(params_path) as f:
        data = json.load(f)
    return new(name=data['name'],
               arg=data['args'],
               label=data['labels'],
               tag=data['tags'],
               **util.dicts.drop(data, ['name', 'args', 'labels', 'tags']))


def restart(*tags, cmd=None, yes=False, only_failed=False):
    """
    restart any arg which is not running and has not logged "exited 0".
    """
    text = params(*tags)
    data = json.loads(text)
    if cmd:
        new_data = json.loads(shell.run(cmd, stdin=text))
        for k in data:
            if data[k] != new_data[k]:
                logging.info('\nold: %s', {k: data[k]})
                logging.info('new: %s', {k: new_data[k]})
        if not yes:
            logging.info('\nwould you like to proceed? y/n\n')
            assert pager.getch() == 'y', 'abort'
        data = new_data
    labels_to_restart = []
    for val in status(*tags):
        state, label = val.split()
        label = label.split('label=', 1)[-1]
        if state == 'failed':
            logging.info('going to restart failed label=%s', label)
            labels_to_restart.append(label)
        elif state == 'missing':
            logging.info('going to restart missing label=%s', label)
            labels_to_restart.append(label)
        elif not only_failed:
            logging.info('going to restart label=%s', label)
            labels_to_restart.append(label)
    if labels_to_restart:
        if not yes:
            logging.info('\nwould you like to proceed? y/n\n')
            assert pager.getch() == 'y', 'abort'
        logging.info('restarting:')
        for label in labels_to_restart:
            logging.info(' %s', label)
        args_to_restart = [arg
                           for arg, label in zip(data['args'], data['labels'])
                           if label in labels_to_restart]
        return new(name=data['name'],
                   arg=args_to_restart,
                   label=labels_to_restart,
                   tag=data['tags'],
                   **util.dicts.drop(data, ['name', 'args', 'labels', 'tags']))
    else:
        logging.info('nothing to restart')


def params(*tags,
           bucket: 's3 bucket to upload logs to' = shell.conf.get_or_prompt_pref('ec2_logs_bucket',  __file__, message='bucket for ec2_logs')):
    launch_id = [x for x in tags if x.startswith('launch=')][0].split('launch=', 1)[-1]
    user = shell.run('whoami')
    return json.dumps(json.loads(shell.run('aws s3 cp s3://%(bucket)s/ec2_logs/%(user)s/launch=%(launch_id)s.json -' % locals())), indent=4)


def status(*tags):
    """
    show all instances, and their state, ie running|done|failed|missing.
    """
    data = json.loads(params(*tags))
    with util.log.disable(''):
        results = [re.split('::', x) for x in logs(*tags, cmd='tail -n1', tail_only=True)]
    fail_labels = [label.split('label=', 1)[-1] for label, _, exit in results if exit != 'exited 0']
    done_labels = [label.split('label=', 1)[-1] for label, _, exit in results if exit == 'exited 0']
    running_labels = [aws.ec2._tags(i)['label'] for i in aws.ec2._ls(tags, state='running')]
    vals = []
    for label in sorted(data['labels']):
        if label in fail_labels:
            vals.append('failed label=%s' % label)
        elif label in done_labels:
            vals.append('done label=%s' % label)
        elif label in running_labels:
            vals.append('running label=%s' % label)
        else:
            vals.append('missing label=%s' % label)
    return sorted(vals, key=lambda x: x.split()[0], reverse=True)


def ls_params(owner=None,
              bucket=shell.conf.get_or_prompt_pref('ec2_logs_bucket',  __file__, message='bucket for ec2_logs')):
    user = owner or shell.run('whoami') # noqa
    vals = ['%(date)sT%(time)s %(name)s' % locals()
            for x in shell.run('aws s3 ls s3://%(bucket)s/ec2_logs/%(user)s/' % locals()).splitlines()
            for name in [x.split()[-1]]
            if name.startswith('launch=') and name.endswith('.json')
            for date, time, _, _ in [x.split()]
            for name in [name.split('.json')[0]]]
    return sorted(vals, reverse=True)


def ls_logs(owner=None,
            bucket=shell.conf.get_or_prompt_pref('ec2_logs_bucket',  __file__, message='bucket for ec2_logs'),
            name_only=False):
    owner = owner or shell.run('whoami')
    prefix = '%(bucket)s/ec2_logs/%(owner)s/' % locals()
    keys = shell.run("aws s3 ls %(prefix)s --recursive" % locals()).splitlines()
    keys = [key for key in keys if 'launch=' in key]
    keys = [key for key in keys if key.endswith('nohup.out')]
    keys = [key.split('/')[-2].split('_') for key in keys]
    keys = [key for key in keys if len(key) == 2]
    keys = [{'date': date,
             'tags': {key: v
                      for x in tags.split(',')
                      if '=' in x
                      for key, v in [x.split('=', 1)]}}
            for date, tags in keys]
    keys = [key for key in keys if 'launch' in key['tags']]
    keys = util.iter.groupby(keys, lambda x: x['tags']['launch'])
    keys = sorted(keys, key=lambda x: x[1][0]['date']) # TODO date should be identical for all launchees, currently is distinct.
    for launch, xs in keys:
        print(xs[0]['tags']['Name'],
              'launch=' + launch,
              'date=' + xs[0]['date'])
        if not name_only:
            print('', *['%(k)s=%(v)s' % locals()
                        for k, v in xs[0]['tags'].items()
                        if k not in ['Name', 'arg', 'label', 'launch', 'nth', 'num']])
            labels = sorted([x['tags']['label'] for x in xs])
            for label in labels:
                print(' ', 'label=' + label)
            print('')


def log(*tags,
        index=-1,
        bucket=shell.conf.get_or_prompt_pref('ec2_logs_bucket',  __file__, message='bucket for ec2_logs'),
        tail_only=False):
    assert tags, 'you must provide some tags'
    owner = shell.run('whoami')
    prefix = '%(bucket)s/ec2_logs/%(owner)s/' % locals()
    keys = shell.run("aws s3 ls %(prefix)s --recursive" % locals()).splitlines()
    keys = [key.split()[-1] for key in keys]
    keys = [key for key in keys if key.endswith('nohup.out.tail' if tail_only else 'nohup.out')]
    keys = [key for key in keys if all(t in key for t in tags)]
    key = keys[index]
    shell.call('aws s3 cp s3://%(bucket)s/%(key)s -' % locals())


def logs(*tags,
         cmd='tail -n 1',
         max_threads=10,
         bucket=shell.conf.get_or_prompt_pref('ec2_logs_bucket',  __file__, message='bucket for ec2_logs'),
         tail_only=False):
    assert tags, 'you must provide some tags'
    owner = shell.run('whoami')
    prefix = '%(bucket)s/ec2_logs/%(owner)s/' % locals()
    keys = shell.run("aws s3 ls %(prefix)s --recursive" % locals()).splitlines()
    keys = [key for key in keys if key.endswith('nohup.out.tail' if tail_only else 'nohup.out')]
    keys = [key.split()[-1] for key in keys]
    keys = [key for key in keys if all(t in key for t in tags)]
    fail = False
    vals = []
    def f(key, cmd, bucket):
        date, tags = key.split('/')[-2].split('_')
        label = [x for x in tags.split(',') if x.startswith('label=')][0]
        try:
            val = '%s::exited 0::%s' % (label, shell.run(('aws s3 cp s3://%(bucket)s/%(key)s - |' + cmd) % locals()))
        except AssertionError:
            val = '%s::exited 1::' % label
            fail = True
        logging.info(val)
        vals.append(val)
    pool.thread.wait(*[(f, [key, cmd, bucket]) for key in keys], max_threads=max_threads)
    if fail:
        sys.exit(1)
    else:
        return sorted(vals)


def main():
    globals()['is_cli'] = True
    shell.ignore_closed_pipes()
    util.log.setup(format='%(message)s')
    with util.log.disable('botocore', 'boto3'):
        try:
            stream = util.hacks.override('--stream')
            with (shell.set_stream() if stream else mock.MagicMock()):
                shell.dispatch_commands(globals(), __name__)
        except AssertionError as e:
            if e.args:
                logging.info(util.colors.red(e.args[0]))
            sys.exit(1)
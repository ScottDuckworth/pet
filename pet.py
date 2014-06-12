#!/usr/bin/env python

from __future__ import print_function

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from syslog import *

if sys.version_info[0] == 2:
  import ConfigParser as configparser
else:
  import configparser

CONFIG_DEFAULTS = {
  'cachedir':         '/var/cache/pet',
  'environmentpath':  '/etc/puppet/environments',
  'puppet':           'puppet',
  'librarian_puppet': 'librarian-puppet',
  'git':              'git',
}

config = configparser.SafeConfigParser(CONFIG_DEFAULTS)
env_rx = re.compile('^[a-z0-9_]+$')
env_forbidden = ('main', 'master', 'agent', 'user')


def check_call(cmd, **kwargs):
  if 'cwd' in kwargs:
    syslog(LOG_DEBUG, "%s cwd=%s" % (cmd, kwargs['cwd']))
  else:
    syslog(LOG_DEBUG, "%s" % (cmd,))
  with open(os.devnull, 'r+b') as devnull:
    return subprocess.check_call(cmd, stdin=devnull, **kwargs)

def check_output(cmd, **kwargs):
  if 'cwd' in kwargs:
    syslog(LOG_DEBUG, "%s cwd=%s" % (cmd, kwargs['cwd']))
  else:
    syslog(LOG_DEBUG, "%s" % (cmd,))
  with open(os.devnull, 'r+b') as devnull:
    return subprocess.check_output(cmd, stdin=devnull, **kwargs)

class PuppetInstance(object):
  def __init__(self, section):
    self.section = section
    key = hashlib.sha256(self.remote.encode()).hexdigest()
    self.remote_cache_path = os.path.join(self.cachedir, key)
    self.librarian_cache_path = os.path.join(self.cachedir, 'librarian-puppet')
    os.environ.setdefault('LIBRARIAN_PUPPET_TMP', self.librarian_cache_path)

  def __getattr__(self, name):
    return config.get(self.section, name)

  def refresh_cache(self):
    syslog(LOG_INFO, "refresh cache")
    if os.path.exists(self.remote_cache_path):
      cmd = [self.git, 'fetch', '--quiet', '--prune']
      check_call(cmd, cwd=self.remote_cache_path)
    else:
      cmd = [self.git, 'clone', '--quiet', '--mirror', self.remote, self.remote_cache_path]
      check_call(cmd)

  def cache_branch_has_commits(self, branch, commits):
    commits = set(commits)
    cmd = [self.git, 'branch', '--list', branch]
    output = check_output(cmd, cwd=self.remote_cache_path)
    if not output:
      return False
    cmd = [self.git, 'rev-list', branch]
    output = check_output(cmd, cwd=self.remote_cache_path)
    for commit in output.splitlines():
      commits.discard(commit)
      if not commits:
        return True
    return False

  def call_backends(self, branches):
    targets = []
    for branch, commits in branches.items():
      targets.append('%s:%s' % (branch, ','.join(commits)))
    for option in config.options(self.section):
      if option == 'backend' or option.startswith('backend.'):
        backend = config.get(self.section, option)
        syslog(LOG_INFO, "calling backend: " + option)
        cmd = shlex.split(backend) + targets
        try:
          check_call(cmd)
        except subprocess.CalledProcessError as e:
          syslog(LOG_ERR, "%s" % (e))

  def active_rev(self, env):
    envpath = os.path.join(self.environmentpath, env)
    cmd = [self.git, 'rev-parse', 'HEAD']
    return check_output(cmd, cwd=envpath).rstrip()

  def cache_rev(self, env):
    cmd = [self.git, 'rev-parse', env]
    return check_output(cmd, cwd=self.remote_cache_path).rstrip()

  def update_environment(self, env):
    envpath = os.path.join(self.environmentpath, env)
    syslog(LOG_INFO, "updating environment %s at %s" % (env, envpath))
    if os.path.exists(envpath):
      old_rev = self.active_rev(env)
      new_rev = self.cache_rev(env)
      if old_rev == new_rev:
        return
      msg = "UPDATE %s from %s to %s" % (env, old_rev[:7], new_rev[:7])
      syslog(LOG_NOTICE, msg)
      print(msg)
      cmd = [self.git, 'pull', '--quiet', self.remote_cache_path, env]
      check_call(cmd, cwd=envpath)
      cmd = [self.git, 'diff', '--name-only', old_rev, new_rev, '--', 'Puppetfile.lock']
      output = check_output(cmd, cwd=envpath)
      if output:
        cmd = [self.librarian_puppet, 'install']
        check_call(cmd, cwd=envpath)
    else:
      msg = "CREATE %s" % env
      syslog(LOG_NOTICE, msg)
      print(msg)
      cmd = [self.git, 'clone', '--quiet', '--branch', env, self.remote_cache_path, envpath]
      check_call(cmd)
      cmd = [self.librarian_puppet, 'install']
      check_call(cmd, cwd=envpath)

  def delete_environment(self, env):
    msg = "DELETE %s" % env
    syslog(LOG_NOTICE, msg)
    print(msg)
    envpath = os.path.join(self.environmentpath, env)
    shutil.rmtree(envpath)

  def local_environments(self):
    return os.listdir(self.environmentpath)

  def remote_environments(self):
    cmd = [self.git, 'branch', '--no-color', '--list']
    output = check_output(cmd, cwd=self.remote_cache_path).decode()
    return [line.lstrip('* ') for line in output.splitlines()]

  def puppet_cmd(self, args):
    cmd = shlex.split(self.puppet) + args
    return subprocess.call(cmd)


def cmd_puppet(pi, args):
  sys.exit(pi.puppet_cmd(args.args))

def cmd_environment_list(pi, args):
  if args.environments:
    for env in sorted(args.environments):
      print("%-14s %s" % (env, pi.active_rev(env)))
  else:
    for env in sorted(pi.local_environments()):
      print("%-14s %s" % (env, pi.active_rev(env)))

def cmd_environment_update(pi, args):
  if args.refresh:
    pi.refresh_cache()
  remote_environments = frozenset(pi.remote_environments())
  if args.environments:
    for env in args.environments:
      if env in remote_environments:
        pi.update_environment(env)
      else:
        pi.delete_environment(env)
  else:
    for env in sorted(remote_environments):
      pi.update_environment(env)
    for env in sorted(pi.local_environments()):
      if env not in remote_environments:
        pi.delete_environment(env)

def cmd_environment_same(pi, args):
  verbose = args.verbose - args.quiet + 1
  env1 = pi.active_rev(args.env1)
  env2 = pi.active_rev(args.env2)
  if env1 == env2:
    if verbose >= 1:
      print("%s and %s are the same" % (args.env1, args.env2))
    return 0
  else:
    if verbose >= 1:
      cmd = [pi.git, 'log', '--pretty=oneline', '%s..%s' % (env2, env1)]
      ahead = check_output(cmd, cwd=pi.remote_cache_path).decode()
      cmd = [pi.git, 'log', '--pretty=oneline', '%s..%s' % (env1, env2)]
      behind = check_output(cmd, cwd=pi.remote_cache_path).decode()
      diff = []
      if ahead:
        diff.append("%d ahead of" % len(ahead.splitlines()))
      if behind:
        diff.append("%d behind" % len(behind.splitlines()))
      print("%s is %s %s" % (
        args.env1,
        " and ".join(diff),
        args.env2,
      ))
      if verbose >= 2:
        if ahead:
          print()
          print("Only in %s:" % args.env1)
          print(ahead.rstrip())
        if behind:
          print()
          print("Only in %s:" % args.env2)
          print(behind.rstrip())
    return 1

def cmd_cgi(pi, args):
  format = args.format
  if format is None:
    user_agent = os.getenv(args.user_agent_env)
    if user_agent is None:
      raise Exception("Undefined user agent")
    user_agent_lc = user_agent.lower()
    if user_agent_lc.find('bitbucket') != -1:
      format = 'bitbucket'
    elif user_agent_lc.find('github') != -1:
      format = 'github'
    else:
      raise Exception("Unknown user agent", user_agent)

  if format == 'bitbucket':
    cgi_bitbucket(pi)
  elif format == 'github':
    cgi_github(pi)
  else:
    raise NotImplementedError("format not handled", format)

def cgi_bitbucket(pi):
  import cgi
  import json
  form = cgi.FieldStorage()
  payload = form['payload'].value
  data = json.loads(payload)
  branches = {}
  for commit in data['commits']:
    commit_rev = commit['raw_node']
    branch = commit['branch']
    if not env_rx.match(branch) or branch in env_forbidden:
      continue
    branches.setdefault(branch, []).append(commit_rev)
  print("Content-Type: text/plain")
  print()
  sys.stdout.flush()
  pi.call_backends(branches)

def cgi_github(pi):
  import json
  data = json.load(sys.stdin)
  ref = data['ref']
  if not ref.startswith('refs/heads/'):
    return
  branch = ref[11:]
  if not env_rx.match(branch) or branch in env_forbidden:
    return
  commits = [commit['sha'] for commit in data['commits']]
  print("Content-Type: text/plain")
  print()
  sys.stdout.flush()
  pi.call_backends({branch: commits})

def cmd_cgi_backend(pi, args):
  if args.targets:
    refreshed = False
    for target in args.targets:
      t = target.split(':', 1)
      env = t[0]
      if len(t) == 1:
        commits = None
        if not refreshed:
          pi.refresh_cache()
          refreshed = True
        pi.update_environment(env)
      elif t[1] == '':
        pi.delete_environment(env)
      else:
        commits = t[1].split(',')
        if not pi.cache_branch_has_commits(env, commits):
          pi.refresh_cache()
          refreshed = True
        pi.update_environment(env)
  else:
    pi.refresh_cache()
    remote_environments = frozenset(pi.remote_environments())
    for env in pi.local_environments():
      if env not in remote_environments:
        pi.delete_environment(env)

def main():
  import argparse

  class SecureStore(argparse.Action):
    def __call__(self, parser, namespace, values, option_string):
      if namespace.secure:
        raise Exception("No further options are allowed")
      setattr(namespace, self.dest, values)

  parser = argparse.ArgumentParser()
  subparsers = parser.add_subparsers()
  parser_puppet = subparsers.add_parser('puppet')
  parser_environment = subparsers.add_parser('environment')
  parser_cgi = subparsers.add_parser('cgi')
  parser_cgi_backend = subparsers.add_parser('cgi-backend')

  subparsers_environment = parser_environment.add_subparsers()
  parser_environment_list = subparsers_environment.add_parser('list')
  parser_environment_update = subparsers_environment.add_parser('update')
  parser_environment_same = subparsers_environment.add_parser('same')

  parser.add_argument('--config', metavar='CONFIG_FILE', action=SecureStore)
  parser.add_argument('--section', default='default', action=SecureStore)
  parser.add_argument('--user', action=SecureStore)
  parser.add_argument('--secure', action='store_true')

  parser_puppet.set_defaults(func=cmd_puppet)
  parser_puppet.add_argument('args', nargs=argparse.REMAINDER)

  parser_cgi.set_defaults(func=cmd_cgi)
  parser_cgi.add_argument('--format', '-f', choices=['bitbucket', 'github'])
  parser_cgi.add_argument('--user-agent-env', metavar='VARIABLE', default='HTTP_USER_AGENT')

  parser_cgi_backend.set_defaults(func=cmd_cgi_backend)
  parser_cgi_backend.add_argument('targets', metavar='TARGET', nargs='*')

  parser_environment_list.set_defaults(func=cmd_environment_list)
  parser_environment_list.add_argument('environments', metavar='ENVIRONMENT', nargs='*')

  parser_environment_update.set_defaults(func=cmd_environment_update)
  parser_environment_update.add_argument('--no-refresh', '-n', dest='refresh', action='store_false', default=True)
  parser_environment_update.add_argument('environments', metavar='ENVIRONMENT', nargs='*')

  parser_environment_same.set_defaults(func=cmd_environment_same)
  parser_environment_same.add_argument('--quiet', '-q', action='count', default=0)
  parser_environment_same.add_argument('--verbose', '-v', action='count', default=0)
  parser_environment_same.add_argument('env1')
  parser_environment_same.add_argument('env2')

  args = parser.parse_args()
  if args.user:
    syslog(LOG_NOTICE, "user=%s" % (args.user,))
  if args.config:
    with open(args.config) as conf:
      config.readfp(conf)
  else:
    config.read([
      '/etc/pet.conf',
      os.path.expanduser('~/.pet.conf'),
    ])
  pi = PuppetInstance(args.section)
  rc = args.func(pi, args)
  sys.exit(rc or 0)


if __name__ == '__main__':
  main()

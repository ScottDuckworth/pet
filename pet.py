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
      cmd = [self.git, 'fetch', '--quiet', '--prune', self.remote]
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
      if commits:
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

  def update_environment(self, env):
    envpath = os.path.join(self.environmentpath, env)
    syslog(LOG_INFO, "updating environment %s at %s" % (env, envpath))
    if os.path.exists(envpath):
      cmd = [self.git, 'rev-parse', 'HEAD']
      old_rev = check_output(cmd, cwd=envpath).rstrip()
      cmd = [self.git, 'rev-parse', env]
      new_rev = check_output(cmd, cwd=self.remote_cache_path).rstrip()
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

def cmd_update(pi, args):
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
  pi.call_backends({branch: commits})

def cmd_cgi_backend(pi, args):
  refreshed = False
  for target in args.targets:
    t = target.split(':', 1)
    branch = t[0]
    if not refreshed:
      if len(t) == 1 or not t[1]:
        pi.refresh_cache()
        refreshed = True
      else:
        commits = t[1].split(',')
        if not pi.cache_branch_has_commits(branch, commits):
          pi.refresh_cache()
          refreshed = True
    pi.update_environment(branch)

def main(argv):
  import argparse

  class SecureStore(argparse.Action):
    def __call__(self, parser, namespace, values, option_string):
      if namespace.secure:
        raise Exception("No further options are allowed")
      setattr(namespace, self.dest, values)

  parser = argparse.ArgumentParser()
  subparsers = parser.add_subparsers()
  parser_puppet = subparsers.add_parser('puppet')
  parser_update = subparsers.add_parser('update')
  parser_cgi = subparsers.add_parser('cgi')
  parser_cgi_backend = subparsers.add_parser('cgi-backend')

  parser.add_argument('--config', metavar='CONFIG_FILE', action=SecureStore)
  parser.add_argument('--section', default='default', action=SecureStore)
  parser.add_argument('--user', action=SecureStore)
  parser.add_argument('--secure', action='store_true')

  parser_puppet.set_defaults(func=cmd_puppet)
  parser_puppet.add_argument('args', nargs=argparse.REMAINDER)

  parser_update.set_defaults(func=cmd_update)
  parser_update.add_argument('--no-refresh', '-n', dest='refresh', action='store_false', default=True)
  parser_update.add_argument('environments', metavar='ENVIRONMENT', nargs='*')

  parser_cgi.set_defaults(func=cmd_cgi)
  parser_cgi.add_argument('--format', '-f', choices=['bitbucket', 'github'])
  parser_cgi.add_argument('--user-agent-env', metavar='VARIABLE', default='HTTP_USER_AGENT')

  parser_cgi_backend.set_defaults(func=cmd_cgi_backend)
  parser_cgi_backend.add_argument('targets', metavar='TARGET', nargs='*')

  args = parser.parse_args(argv)
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
  args.func(pi, args)


if __name__ == '__main__':
  main(sys.argv[1:])

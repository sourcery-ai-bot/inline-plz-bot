#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import hashlib
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from flask import Flask, request, redirect
import github3

app = Flask(__name__)

SAFE_ENV = os.environ.copy()
# wipe our token before passing our envvars into inline-plz
SAFE_ENV['TOKEN'] = ''
DOTFILES = 'dotfiles'
STOP_FILE_NAME = '.inlineplzstop'
SSH_FILE_NAME = 'inline_plz_rsa'
SSH_FILE_PATH = os.path.join(os.path.expanduser('~'), '.ssh', SSH_FILE_NAME)
REVIEWS_IN_PROGRESS = dict()
SSH_LOCK = threading.Lock()


def ssh_keygen():
    time.sleep(random.randint(1, 10))
    while not os.path.exists(SSH_FILE_PATH):
        try:
            subprocess.check_call(['ssh-keygen', '-t', 'rsa', '-b', '2048', '-f', SSH_FILE_PATH, '-q', '-N', ''])
            ssh_output = subprocess.check_output('ssh-agent -s', shell=True, stderr=subprocess.STDOUT)
            # http://code.activestate.com/recipes/533143-set-environment-variables-for-using-ssh-in-python-/
            for sh_line in ssh_output.splitlines():
                matches=re.search("(\S+)\=(\S+)\;", sh_line)
                if matches:
                    os.environ[matches.group(1)]=matches.group(2)
                    SAFE_ENV[matches.group(1)]=matches.group(2)
            subprocess.check_call('ssh-add {}'.format(SSH_FILE_PATH), shell=True)
        except Exception:
            traceback.print_exc()
            time.sleep(random.randint(1, 10))


TRUSTED = os.environ.get('TRUSTED', '').lower().strip() in ['true', 'yes', '1']
if TRUSTED:
    ssh_keygen()

SSH_KEY_HASH = hashlib.md5()
SSH_KEY_HASH.update(open(SSH_FILE_PATH).read())
SSH_KEY_HASH = SSH_KEY_HASH.hexdigest()[-6:]


@app.errorhandler(Exception)
def all_exception_handler():
    """Catch, print, then ignore all errors."""
    traceback.print_exc()


def clone(url, dir, token, ref=None):
    # https://github.com/blog/1270-easier-builds-and-deployments-using-git-over-https-and-oauth
    url = url.replace('https://', 'https://{}@'.format(token))
    print('Cloning: {}'.format(url))
    try:
        os.makedirs(dir)
    except OSError:
        pass
    try:
        subprocess.check_call(
            ['git', 'init'],
            cwd=dir, env=SAFE_ENV
        )

        pull_cmd = ['git', 'pull', url]
        if ref:
            pull_cmd.append(ref)
        subprocess.check_call(
            pull_cmd,
            cwd=dir, env=SAFE_ENV
        )
        return True
    except subprocess.CalledProcessError:
        return False


def clone_dotfiles(url, org, tempdir, token):
    # https://github.com/blog/1270-easier-builds-and-deployments-using-git-over-https-and-oauth
    clone_url = '/'.join([url, org, DOTFILES]) + '.git'
    print('Cloning: {}'.format(clone_url))
    dotfile_path = os.path.join(tempdir, DOTFILES)
    return clone(clone_url, dotfile_path, token)


def ssh_setup(url, token):
    with SSH_LOCK:
        try:
            with open(os.path.join(os.path.expanduser('~'), '.ssh', 'config'), 'ar+') as sshconfig:
                contents = sshconfig.read()
                if not 'HostName {}'.format(url) in contents:
                    sshconfig.write('\nHost {0}\n\tHostName {0}\n\tIdentityFile {1}'.format(url, SSH_FILE_PATH))
        except Exception:
            traceback.print_exc()
        if not url or url in ['http://github.com', 'https://github.com']:
            github = github3.GitHub(token=token)
        else:
            github = github3.GitHubEnterprise(url, token=token)

        key_found = False
        for key in github.iter_keys():
            if SSH_FILE_NAME in key.title and not SSH_KEY_HASH in key.title:
                github.delete_key(key.id)
            elif key.title == '{}_{}'.format(SSH_FILE_NAME, SSH_KEY_HASH):
                key_found = True
        if not key_found:
            github.create_key('{}_{}'.format(SSH_FILE_NAME, SSH_KEY_HASH), open(SSH_FILE_PATH + '.pub').read())

        keygen_url = url.split('//')[-1]
        try:
            output = subprocess.check_output(['ssh-keygen', '-F', keygen_url], stderr=subprocess.STDOUT)
            if output.strip():
                return
        except subprocess.CalledProcessError:
            traceback.print_exc()
        try:
            output = subprocess.check_output(['ssh-keyscan', '-t', 'rsa', keygen_url], stderr=subprocess.STDOUT)
            with open(os.path.join(os.path.expanduser('~'), '.ssh', 'known_hosts'), 'a') as known_hosts:
                known_hosts.write(output)
        except Exception:
            traceback.print_exc()


def lint(data):
    try:
        pull_request = data['pull_request']['number']
        repo_slug = data['repository']['full_name']
        name = data['repository']['name']
        token = os.environ.get('TOKEN')
        interface = 'github'
        url = 'https://' + data['repository']['ssh_url'].split('@')[1].split(':')[0]
        event_type = data['action']
        sha = data['pull_request']['head']['sha']
        ref = data['pull_request']['head']['ref']
        clone_url = data['pull_request']['head']['repo']['clone_url']
        org = data['repository']['owner']['login']
    except KeyError:
        traceback.print_exc()
        return 'Invalid pull request data.'


    print('Starting inline-plz:')
    print('Event: {}'.format(event_type))
    print('PR: {}'.format(pull_request))
    print('Repo slug: {}'.format(repo_slug))
    print('Name: {}'.format(name))
    print('SHA: {}'.format(sha))
    print('Clone URL: {}'.format(clone_url))

    if event_type not in ['opened', 'synchronize']:
        return

    if TRUSTED:
        ssh_setup(url, token)

    # make temp dirs
    tempdir = tempfile.mkdtemp()
    dotfile_dir = tempfile.mkdtemp()
    time.sleep(1)

    # check for existing runs against this PR
    pr_name = '{}-{}'.format(repo_slug, pull_request)
    REVIEWS_IN_PROGRESS.setdefault(pr_name, set())
    for dir in REVIEWS_IN_PROGRESS[pr_name]:
        stopfile_path = os.path.join(dir, STOP_FILE_NAME)
        try:
            open(stopfile_path, 'w').close()
        except (IOError, OSError):
            pass
    REVIEWS_IN_PROGRESS[pr_name].add(tempdir)

    try:
        # git clone into temp dir
        clone(clone_url, os.path.join(tempdir, name), token, ref)
        time.sleep(1)

        # git checkout our sha
        subprocess.check_call(
            ['git', 'checkout', sha],
            cwd=os.path.join(tempdir, name), env=SAFE_ENV
        )
        time.sleep(1)

        args = [
            'inline-plz',
            '--autorun',
            '--repo-slug={}'.format(repo_slug),
            '--pull-request={}'.format(pull_request),
            '--url={}'.format(url),
            '--token={}'.format(token),
            '--interface={}'.format(interface),
            '--zero-exit'
        ]
        if TRUSTED:
            args.append('--trusted')
        if clone_dotfiles(url, org, dotfile_dir, token):
            args.append('--config-dir={}'.format(
                os.path.join(dotfile_dir, 'dotfiles')
            ))
        time.sleep(1)

        # run inline-plz in temp dir
        print('Args: {}'.format(args))
        subprocess.check_call(args, cwd=os.path.join(tempdir, name), env=SAFE_ENV)
        time.sleep(1)
    finally:
        # delete temp dir
        time.sleep(1)
        shutil.rmtree(tempdir, ignore_errors=True)
        shutil.rmtree(dotfile_dir, ignore_errors=True)
        REVIEWS_IN_PROGRESS[pr_name].discard(tempdir)


@app.route('/', methods=['GET', 'POST'])
def root():
    if request.method == 'GET':
        return redirect('https://github.com/guykisel/inline-plz-bot', code=302)

    # https://developer.github.com/v3/activity/events/types/#pullrequestevent
    data = request.get_json()
    lint_thread = threading.Thread(target=lint, args=(data, ))
    lint_thread.start()
    return 'Success!'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
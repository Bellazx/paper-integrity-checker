#!/usr/bin/env python3
"""Kernel egress fence for one background test and all its descendants (cgroup v1)."""
import argparse
import os
from pathlib import Path
import subprocess

GROUP = Path('/sys/fs/cgroup/net_cls/paper-submissions-test')
CLASS_ID = '0x519928'
RULE = ['-m', 'cgroup', '--cgroup', CLASS_ID, '-d', '10.119.5.44/32',
        '-m', 'comment', '--comment', 'paper-submissions-test-no-db',
        '-j', 'REJECT', '--reject-with', 'icmp-admin-prohibited']


def firewall(operation, check=True):
    return subprocess.run(['/usr/sbin/iptables', '-w', '10', operation, 'OUTPUT', *RULE],
                          check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['run', 'cleanup'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Network guard requires root')
    if args.mode == 'cleanup':
        if GROUP.exists() and (GROUP / 'cgroup.procs').read_text().strip():
            raise RuntimeError('Cannot remove network guard while test processes remain')
        if firewall('-C', check=False).returncode == 0:
            firewall('-D')
        if GROUP.exists():
            GROUP.rmdir()
        return
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        raise RuntimeError('Missing test command')
    GROUP.mkdir(exist_ok=True)
    if (GROUP / 'cgroup.procs').read_text().strip():
        raise RuntimeError('Another guarded test is already running')
    (GROUP / 'net_cls.classid').write_text(CLASS_ID)
    if firewall('-C', check=False).returncode != 0:
        firewall('-I')
    (GROUP / 'cgroup.procs').write_text(str(os.getpid()))
    # runuser supplies the library account's normal HOME/authentication context;
    # setpriv then prevents descendants from acquiring elevated privileges.
    os.execv('/usr/sbin/runuser', ['runuser', '-u', 'library', '--',
        '/usr/bin/setpriv', '--no-new-privs', *command])


if __name__ == '__main__':
    main()

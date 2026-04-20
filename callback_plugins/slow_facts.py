#!/usr/bin/env python
# -*- coding: utf-8 -*-
# (c) Ansible callback plugin — slow_facts
#
# Warns when a host is taking longer than expected to gather facts.
# Prints a live warning mid-task rather than waiting for the task to finish.
#
# Install:
#   Place in callback_plugins/ next to your playbook, or in
#   ~/.ansible/plugins/callback/, or set callback_plugins in ansible.cfg.
#
# Enable in ansible.cfg:
#   [defaults]
#   callbacks_enabled = slow_facts
#
# Configuration (ansible.cfg or environment variables):
#   [callback_slow_facts]
#   warn_seconds   = 10     # seconds before first warning   (SLOW_FACTS_WARN_SECONDS)
#   repeat_seconds = 15     # seconds between repeat warnings (SLOW_FACTS_REPEAT_SECONDS)
#   critical_seconds = 60   # seconds before critical warning (SLOW_FACTS_CRITICAL_SECONDS)
#   show_elapsed   = true   # show elapsed time in warnings   (SLOW_FACTS_SHOW_ELAPSED)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = '''
    name: slow_facts
    type: notification
    short_description: Warn when gather_facts is taking a long time
    description:
        - Monitors the gather_facts / setup task per host.
        - Prints a warning to stdout when a host has been gathering facts
          for longer than the configured threshold.
        - Prints an escalating critical warning beyond a second threshold.
        - Uses a background thread to emit live warnings without blocking.
    requirements:
        - Python threading module (stdlib)
    options:
        warn_seconds:
            description: Seconds of fact-gathering before the first warning.
            default: 10
            env:
                - name: SLOW_FACTS_WARN_SECONDS
            ini:
                - section: callback_slow_facts
                  key: warn_seconds
        repeat_seconds:
            description: Seconds between repeated warnings for the same host.
            default: 15
            env:
                - name: SLOW_FACTS_REPEAT_SECONDS
            ini:
                - section: callback_slow_facts
                  key: repeat_seconds
        critical_seconds:
            description: Seconds before upgrading to a critical-level warning.
            default: 60
            env:
                - name: SLOW_FACTS_CRITICAL_SECONDS
            ini:
                - section: callback_slow_facts
                  key: critical_seconds
        show_elapsed:
            description: Include elapsed time in warning messages.
            default: true
            type: bool
            env:
                - name: SLOW_FACTS_SHOW_ELAPSED
            ini:
                - section: callback_slow_facts
                  key: show_elapsed
'''

import threading
import time
import sys
import os

from ansible.plugins.callback import CallbackBase
from ansible.utils.color import colorize, hostcolor

try:
    from ansible.utils.display import Display
    display = Display()
except ImportError:
    display = None


def _print(msg, color=None):
    """Write directly to stdout with optional ANSI colour."""
    COLORS = {
        'yellow':  '\033[33m',
        'red':     '\033[31m',
        'cyan':    '\033[36m',
        'reset':   '\033[0m',
        'bold':    '\033[1m',
    }
    if color and sys.stdout.isatty():
        msg = COLORS.get(color, '') + msg + COLORS['reset']
    # flush immediately so it appears mid-task
    sys.stdout.write('\n' + msg + '\n')
    sys.stdout.flush()


class SlowFactsWatcher(threading.Thread):
    """
    Background thread that watches a single host's fact-gather start time
    and emits warnings at configured thresholds.
    """

    def __init__(self, host, start_time, warn_seconds, repeat_seconds,
                 critical_seconds, show_elapsed):
        super(SlowFactsWatcher, self).__init__(daemon=True)
        self.host             = host
        self.start_time       = start_time
        self.warn_seconds     = warn_seconds
        self.repeat_seconds   = repeat_seconds
        self.critical_seconds = critical_seconds
        self.show_elapsed     = show_elapsed
        self._stop_event      = threading.Event()
        self._warned          = False

    def stop(self):
        self._stop_event.set()

    def elapsed(self):
        return time.time() - self.start_time

    def run(self):
        # Sleep until first warn threshold
        self._stop_event.wait(timeout=self.warn_seconds)
        if self._stop_event.is_set():
            return  # facts finished before first threshold — all good

        # First warning
        elapsed = self.elapsed()
        self._emit_warning(elapsed, critical=False, first=True)
        self._warned = True

        # Keep warning every repeat_seconds until stopped or critical
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.repeat_seconds)
            if self._stop_event.is_set():
                break
            elapsed = self.elapsed()
            critical = elapsed >= self.critical_seconds
            self._emit_warning(elapsed, critical=critical, first=False)

    def _emit_warning(self, elapsed, critical=False, first=False):
        elapsed_str = ''
        if self.show_elapsed:
            elapsed_str = ' [{:.0f}s elapsed]'.format(elapsed)

        if critical:
            msg = (
                '[slow_facts] CRITICAL: {} has been gathering facts for '
                '{:.0f}s — possible hang, unreachable, or very slow host{}'.format(
                    self.host, elapsed, elapsed_str)
            )
            _print(msg, color='red')
        else:
            level = 'WARNING' if first else 'STILL WAITING'
            msg = (
                '[slow_facts] {}: {} is still gathering facts{}'.format(
                    level, self.host, elapsed_str)
            )
            _print(msg, color='yellow')

    def did_warn(self):
        return self._warned


class CallbackModule(CallbackBase):
    """
    Ansible callback plugin: slow_facts
    Monitors gather_facts tasks and warns when a host is taking too long.
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE    = 'notification'
    CALLBACK_NAME    = 'slow_facts'
    CALLBACK_NEEDS_WHITELIST = True

    # Task names that correspond to fact gathering
    GATHER_FACTS_TASKS = frozenset([
        'gathering facts',
        'setup',
        'ansible.builtin.setup',
        'ansible.builtin.gather_facts',
        'gather_facts',
    ])

    def __init__(self):
        super(CallbackModule, self).__init__()
        self._watchers   = {}   # host -> SlowFactsWatcher
        self._task       = None
        self._is_gather  = False
        self._lock       = threading.Lock()

        # Defaults — overridden by set_options()
        self._warn_seconds     = 10
        self._repeat_seconds   = 15
        self._critical_seconds = 60
        self._show_elapsed     = True

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super(CallbackModule, self).set_options(
            task_keys=task_keys,
            var_options=var_options,
            direct=direct,
        )
        self._warn_seconds     = int(self.get_option('warn_seconds'))
        self._repeat_seconds   = int(self.get_option('repeat_seconds'))
        self._critical_seconds = int(self.get_option('critical_seconds'))
        self._show_elapsed     = bool(self.get_option('show_elapsed'))

    # ── Task lifecycle ────────────────────────────────────────────────────────

    def v2_playbook_on_task_start(self, task, is_conditional):
        task_name = (task.get_name() or '').strip().lower()
        self._is_gather = task_name in self.GATHER_FACTS_TASKS
        self._task = task

        # If previous task had watchers still running, stop them
        if not self._is_gather:
            self._stop_all_watchers()

    def v2_runner_on_start(self, host, task):
        """Called when Ansible starts executing a task on a specific host."""
        if not self._is_gather:
            return

        host_name = host.get_name()
        watcher = SlowFactsWatcher(
            host=host_name,
            start_time=time.time(),
            warn_seconds=self._warn_seconds,
            repeat_seconds=self._repeat_seconds,
            critical_seconds=self._critical_seconds,
            show_elapsed=self._show_elapsed,
        )
        with self._lock:
            self._watchers[host_name] = watcher
        watcher.start()

    # ── Runner result handlers — stop the watcher when host finishes ──────────

    def v2_runner_on_ok(self, result):
        self._stop_watcher(result._host.get_name(), outcome='ok')

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._stop_watcher(result._host.get_name(), outcome='failed')

    def v2_runner_on_unreachable(self, result):
        host_name = result._host.get_name()
        # For unreachable hosts during fact gather — escalate message
        watcher = self._watchers.get(host_name)
        if watcher:
            elapsed = watcher.elapsed()
            msg = (
                '[slow_facts] UNREACHABLE: {} became unreachable after '
                '{:.1f}s of fact gathering'.format(host_name, elapsed)
            )
            _print(msg, color='red')
        self._stop_watcher(host_name, outcome='unreachable')

    def v2_runner_on_skipped(self, result):
        self._stop_watcher(result._host.get_name(), outcome='skipped')

    # ── Playbook end — clean up any lingering watchers ────────────────────────

    def v2_playbook_on_stats(self, stats):
        self._stop_all_watchers()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _stop_watcher(self, host_name, outcome='ok'):
        with self._lock:
            watcher = self._watchers.pop(host_name, None)
        if watcher is None:
            return
        watcher.stop()
        watcher.join(timeout=1)

        if watcher.did_warn() and outcome == 'ok':
            elapsed = watcher.elapsed()
            msg = (
                '[slow_facts] RESOLVED: {} finished gathering facts '
                'after {:.1f}s'.format(host_name, elapsed)
            )
            _print(msg, color='cyan')

    def _stop_all_watchers(self):
        with self._lock:
            watchers = dict(self._watchers)
            self._watchers.clear()
        for host_name, watcher in watchers.items():
            watcher.stop()
            watcher.join(timeout=1)

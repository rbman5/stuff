#!/usr/bin/env python
# -*- coding: utf-8 -*-
# (c) Ansible callback plugin — http_notify
#
# Posts per-host playbook results to an HTTP endpoint when a play finishes.
# Includes full task name, error message, module args, and host vars on failure.
#
# Install:
#   Place in callback_plugins/ next to your playbook, or in
#   ~/.ansible/plugins/callback/
#
# Enable in ansible.cfg:
#   [defaults]
#   callbacks_enabled = http_notify
#
# Configuration (ansible.cfg — NO inline # comments allowed after values):
#   [callback_http_notify]
#   url              = https://hooks.example.com/ansible
#   token            = secret-bearer-token
#   post_on          = all
#   timeout          = 10
#   verify_ssl       = true
#   include_ok       = false
#   include_skipped  = false
#   include_vars     = false
#   max_msg_len      = 2000
#
# Environment variable equivalents:
#   HTTP_NOTIFY_URL, HTTP_NOTIFY_TOKEN, HTTP_NOTIFY_POST_ON,
#   HTTP_NOTIFY_TIMEOUT, HTTP_NOTIFY_VERIFY_SSL,
#   HTTP_NOTIFY_INCLUDE_OK, HTTP_NOTIFY_INCLUDE_SKIPPED,
#   HTTP_NOTIFY_INCLUDE_VARS, HTTP_NOTIFY_MAX_MSG_LEN
#
# post_on values:
#   all      — post after every play regardless of outcome
#   failure  — post only when at least one host failed or was unreachable
#   always   — alias for all
#
# Payload structure (JSON):
# {
#   "playbook":   "site.yml",
#   "play":       "Deploy web servers",
#   "status":     "failure",          # success | failure | unreachable
#   "started_at": "2026-04-19T06:00:00Z",
#   "finished_at":"2026-04-19T06:01:23Z",
#   "duration_s": 83,
#   "hosts": {
#     "web01": {
#       "status":       "failed",
#       "failed_task":  "Install nginx",
#       "failed_module":"ansible.builtin.package",
#       "error":        "No package nginx available.",
#       "result":       { ...full task result dict... },
#       "ok":           12,
#       "changed":      3,
#       "failures":     1,
#       "unreachable":  0,
#       "skipped":      2,
#       "rescued":      0,
#       "ignored":      0,
#       "tasks": [
#         { "task": "Gather facts", "status": "ok",      "changed": false, "duration_s": 1.2 },
#         { "task": "Install nginx","status": "failed",  "changed": false, "duration_s": 3.4,
#           "error": "No package nginx available." }
#       ]
#     },
#     "web02": { "status": "ok", ... }
#   }
# }

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = '''
    name: http_notify
    type: notification
    short_description: POST per-host playbook results to an HTTP endpoint
    description:
        - Collects task-level results for every host throughout a play.
        - On play end, POSTs a JSON summary to a configured HTTP endpoint.
        - Includes the failing task name, module, error message and full result.
        - Configurable to post on all runs or only on failure.
    options:
        url:
            description: HTTP endpoint to POST results to.
            required: true
            env:
                - name: HTTP_NOTIFY_URL
            ini:
                - section: callback_http_notify
                  key: url
        token:
            description: Bearer token sent in the Authorization header.
            default: ""
            env:
                - name: HTTP_NOTIFY_TOKEN
            ini:
                - section: callback_http_notify
                  key: token
        post_on:
            description: When to post. One of all, always, failure.
            default: all
            env:
                - name: HTTP_NOTIFY_POST_ON
            ini:
                - section: callback_http_notify
                  key: post_on
        timeout:
            description: HTTP request timeout in seconds.
            default: 10
            env:
                - name: HTTP_NOTIFY_TIMEOUT
            ini:
                - section: callback_http_notify
                  key: timeout
        verify_ssl:
            description: Verify SSL certificates.
            default: true
            type: bool
            env:
                - name: HTTP_NOTIFY_VERIFY_SSL
            ini:
                - section: callback_http_notify
                  key: verify_ssl
        include_ok:
            description: Include per-task entries for successful tasks.
            default: false
            type: bool
            env:
                - name: HTTP_NOTIFY_INCLUDE_OK
            ini:
                - section: callback_http_notify
                  key: include_ok
        include_skipped:
            description: Include per-task entries for skipped tasks.
            default: false
            type: bool
            env:
                - name: HTTP_NOTIFY_INCLUDE_SKIPPED
            ini:
                - section: callback_http_notify
                  key: include_skipped
        include_vars:
            description: Include host vars snapshot in failure payloads.
            default: false
            type: bool
            env:
                - name: HTTP_NOTIFY_INCLUDE_VARS
            ini:
                - section: callback_http_notify
                  key: include_vars
        max_msg_len:
            description: Truncate error messages to this many characters (0 = unlimited).
            default: 2000
            env:
                - name: HTTP_NOTIFY_MAX_MSG_LEN
            ini:
                - section: callback_http_notify
                  key: max_msg_len
'''

import json
import time
import traceback
import sys

from datetime import datetime, timezone
from ansible.plugins.callback import CallbackBase

try:
    import urllib.request as urlrequest
    import urllib.error as urlerror
except ImportError:
    import urllib2 as urlrequest
    import urllib2 as urlerror


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utcnow():
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _clean_int(value):
    if isinstance(value, int):
        return value
    return int(str(value).split('#')[0].split(';')[0].strip())


def _clean_bool(value):
    if isinstance(value, bool):
        return value
    cleaned = str(value).split('#')[0].split(';')[0].strip().lower()
    return cleaned in ('true', '1', 'yes', 'on')


def _clean_str(value):
    return str(value).split('#')[0].split(';')[0].strip()


def _safe_serialize(obj, max_depth=5, _depth=0):
    """
    Recursively convert an object to a JSON-serializable form.
    Handles AnsibleUnsafeText, Results objects, and other non-standard types.
    """
    if _depth > max_depth:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, bytes):
        return obj.decode('utf-8', errors='replace')
    if isinstance(obj, dict):
        return {
            str(k): _safe_serialize(v, max_depth, _depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(i, max_depth, _depth + 1) for i in obj]
    # Ansible types that inherit from str (AnsibleUnsafeText etc.)
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def _extract_error(result_dict):
    """
    Pull the most useful human-readable error string from a task result dict.
    Tries common keys in order of usefulness.
    """
    for key in ('msg', 'message', 'stderr', 'stdout', 'reason', 'module_stderr'):
        val = result_dict.get(key)
        if val:
            return str(val)
    # Last resort — return a cleaned JSON snippet of the result
    try:
        trimmed = {
            k: v for k, v in result_dict.items()
            if k not in ('invocation', '_ansible_verbose_always',
                         '_ansible_no_log', '_ansible_parsed')
        }
        return json.dumps(trimmed, default=str)[:500]
    except Exception:
        return repr(result_dict)[:500]


# ── Per-host state ────────────────────────────────────────────────────────────

class HostState:
    """Tracks all task results for a single host within a play."""

    def __init__(self, name):
        self.name         = name
        self.status       = 'ok'          # ok | failed | unreachable | skipped
        self.tasks        = []            # list of task entry dicts
        self.failed_task  = None          # name of the first failing task
        self.failed_module = None
        self.error        = None          # error string from first failure
        self.full_result  = None          # full result dict from first failure
        self.host_vars    = {}

    def record(self, task_name, module_name, status, changed,
               duration_s, error=None, result_dict=None, include_ok=False,
               include_skipped=False):

        entry = {
            'task':       task_name,
            'module':     module_name,
            'status':     status,
            'changed':    changed,
            'duration_s': round(duration_s, 2),
        }
        if error:
            entry['error'] = error

        should_append = (
            status in ('failed', 'unreachable')
            or (status == 'ok'      and include_ok)
            or (status == 'skipped' and include_skipped)
            or (status == 'changed' and include_ok)
        )
        if should_append:
            self.tasks.append(entry)

        # Capture first failure details
        if status in ('failed', 'unreachable') and self.failed_task is None:
            self.status        = status
            self.failed_task   = task_name
            self.failed_module = module_name
            self.error         = error
            self.full_result   = result_dict

    def to_dict(self, stats, include_vars=False):
        d = {
            'status':      self.status,
            'ok':          stats.get('ok', 0),
            'changed':     stats.get('changed', 0),
            'failures':    stats.get('failures', 0),
            'unreachable': stats.get('dark', 0),
            'skipped':     stats.get('skipped', 0),
            'rescued':     stats.get('rescued', 0),
            'ignored':     stats.get('ignored', 0),
            'tasks':       self.tasks,
        }
        if self.failed_task:
            d['failed_task']   = self.failed_task
            d['failed_module'] = self.failed_module
            d['error']         = self.error
            d['result']        = _safe_serialize(self.full_result or {})
        if include_vars and self.host_vars:
            d['vars'] = _safe_serialize(self.host_vars)
        return d


# ── Callback ──────────────────────────────────────────────────────────────────

class CallbackModule(CallbackBase):

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE    = 'notification'
    CALLBACK_NAME    = 'http_notify'
    CALLBACK_NEEDS_WHITELIST = True

    def __init__(self):
        super(CallbackModule, self).__init__()

        # Options (overridden by set_options)
        self._url             = ''
        self._token           = ''
        self._post_on         = 'all'
        self._timeout         = 10
        self._verify_ssl      = True
        self._include_ok      = False
        self._include_skipped = False
        self._include_vars    = False
        self._max_msg_len     = 2000

        # Runtime state
        self._playbook_name   = ''
        self._play_name       = ''
        self._play_started    = None
        self._current_task    = None
        self._task_started    = None
        self._host_states     = {}   # host_name -> HostState

    # ── Option loading ────────────────────────────────────────────────────────

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super(CallbackModule, self).set_options(
            task_keys=task_keys,
            var_options=var_options,
            direct=direct,
        )
        self._url             = _clean_str(self.get_option('url'))
        self._token           = _clean_str(self.get_option('token'))
        self._post_on         = _clean_str(self.get_option('post_on')).lower()
        self._timeout         = _clean_int(self.get_option('timeout'))
        self._verify_ssl      = _clean_bool(self.get_option('verify_ssl'))
        self._include_ok      = _clean_bool(self.get_option('include_ok'))
        self._include_skipped = _clean_bool(self.get_option('include_skipped'))
        self._include_vars    = _clean_bool(self.get_option('include_vars'))
        self._max_msg_len     = _clean_int(self.get_option('max_msg_len'))

    # ── Playbook / play lifecycle ─────────────────────────────────────────────

    def v2_playbook_on_start(self, playbook):
        self._playbook_name = getattr(playbook, '_file_name', '') or ''

    def v2_playbook_on_play_start(self, play):
        self._play_name    = play.get_name().strip()
        self._play_started = time.time()
        self._host_states  = {}

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._current_task  = task
        self._task_started  = time.time()

    def v2_playbook_on_handler_task_start(self, task):
        self._current_task = task
        self._task_started = time.time()

    # ── Task result handlers ──────────────────────────────────────────────────

    def v2_runner_on_ok(self, result):
        self._record(result, status='changed' if result.is_changed() else 'ok')

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._record(result, status='failed', ignore_errors=ignore_errors)

    def v2_runner_on_unreachable(self, result):
        self._record(result, status='unreachable')

    def v2_runner_on_skipped(self, result):
        self._record(result, status='skipped')

    # ── Stats / play end ──────────────────────────────────────────────────────

    def v2_playbook_on_stats(self, stats):
        finished_at  = _utcnow()
        duration_s   = round(time.time() - (self._play_started or time.time()), 1)
        started_at   = datetime.fromtimestamp(
            self._play_started or time.time(), tz=timezone.utc
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Build per-host summary
        hosts_summary = {}
        any_failure   = False
        any_unreach   = False

        for host_name, state in self._host_states.items():
            host_stats = self._get_host_stats(stats, host_name)
            hosts_summary[host_name] = state.to_dict(
                host_stats, include_vars=self._include_vars
            )
            if state.status == 'failed':
                any_failure = True
            if state.status == 'unreachable':
                any_unreach = True

        # Also add hosts that had zero tasks recorded (all ok / no tasks)
        for host_name in stats.processed.keys():
            if host_name not in hosts_summary:
                host_stats = self._get_host_stats(stats, host_name)
                status = 'ok'
                if host_stats.get('failures', 0) > 0:
                    status = 'failed'
                    any_failure = True
                if host_stats.get('dark', 0) > 0:
                    status = 'unreachable'
                    any_unreach = True
                hosts_summary[host_name] = {
                    'status':      status,
                    'ok':          host_stats.get('ok', 0),
                    'changed':     host_stats.get('changed', 0),
                    'failures':    host_stats.get('failures', 0),
                    'unreachable': host_stats.get('dark', 0),
                    'skipped':     host_stats.get('skipped', 0),
                    'rescued':     host_stats.get('rescued', 0),
                    'ignored':     host_stats.get('ignored', 0),
                    'tasks':       [],
                }

        overall_status = 'success'
        if any_unreach:
            overall_status = 'unreachable'
        if any_failure:
            overall_status = 'failure'

        # Decide whether to post
        should_post = (
            self._post_on in ('all', 'always')
            or (self._post_on == 'failure' and overall_status != 'success')
        )

        if not should_post:
            return

        payload = {
            'playbook':    self._playbook_name,
            'play':        self._play_name,
            'status':      overall_status,
            'started_at':  started_at,
            'finished_at': finished_at,
            'duration_s':  duration_s,
            'hosts':       hosts_summary,
        }

        self._post(payload)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_host_state(self, host_name):
        if host_name not in self._host_states:
            self._host_states[host_name] = HostState(host_name)
        return self._host_states[host_name]

    def _get_host_stats(self, stats, host_name):
        """Extract stats dict for a single host from the stats object."""
        try:
            return {
                'ok':          stats.summarize(host_name).get('ok', 0),
                'changed':     stats.summarize(host_name).get('changed', 0),
                'failures':    stats.summarize(host_name).get('failures', 0),
                'dark':        stats.summarize(host_name).get('dark', 0),
                'skipped':     stats.summarize(host_name).get('skipped', 0),
                'rescued':     stats.summarize(host_name).get('rescued', 0),
                'ignored':     stats.summarize(host_name).get('ignored', 0),
            }
        except Exception:
            return {}

    def _record(self, result, status, ignore_errors=False):
        """Extract task metadata from a result and store it on the host state."""
        host_name   = result._host.get_name()
        task        = self._current_task
        task_name   = (task.get_name() if task else '') or 'unknown'
        module_name = (task.action if task else '') or 'unknown'
        result_dict = result._result or {}

        # Capture host vars for failure enrichment
        if status in ('failed', 'unreachable') and self._include_vars:
            try:
                state = self._get_host_state(host_name)
                if not state.host_vars:
                    state.host_vars = dict(result._host.vars or {})
            except Exception:
                pass

        # Build error string
        error = None
        if status in ('failed', 'unreachable'):
            error = _extract_error(result_dict)
            if self._max_msg_len and len(error) > self._max_msg_len:
                error = error[:self._max_msg_len] + '… [truncated]'

        duration_s = round(time.time() - (self._task_started or time.time()), 2)

        # If ignore_errors is set, downgrade failed to ok for status tracking
        effective_status = status
        if status == 'failed' and ignore_errors:
            effective_status = 'ignored'

        self._get_host_state(host_name).record(
            task_name    = task_name,
            module_name  = module_name,
            status       = effective_status,
            changed      = result.is_changed(),
            duration_s   = duration_s,
            error        = error,
            result_dict  = _safe_serialize(result_dict),
            include_ok   = self._include_ok,
            include_skipped = self._include_skipped,
        )

    def _post(self, payload):
        """POST the payload as JSON to the configured URL."""
        if not self._url:
            self._display.warning(
                'http_notify: no url configured — skipping POST'
            )
            return

        try:
            body = json.dumps(payload, default=str).encode('utf-8')
            headers = {
                'Content-Type': 'application/json',
                'User-Agent':   'ansible-http-notify/1.0',
            }
            if self._token:
                headers['Authorization'] = 'Bearer ' + self._token

            req = urlrequest.Request(
                url     = self._url,
                data    = body,
                headers = headers,
                method  = 'POST',
            )

            # SSL context for verify_ssl=false
            ctx = None
            if not self._verify_ssl:
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode    = ssl.CERT_NONE

            with urlrequest.urlopen(req, timeout=self._timeout, context=ctx) as resp:
                status_code = resp.getcode()
                self._display.vv(
                    'http_notify: POST {} returned HTTP {}'.format(
                        self._url, status_code)
                )

        except Exception as exc:
            self._display.warning(
                'http_notify: failed to POST to {}: {}'.format(
                    self._url, exc)
            )

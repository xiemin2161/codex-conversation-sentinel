#!/usr/bin/env python3
"""Read compaction events and keep an evidence-based handoff state machine."""
import argparse
import errno
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid
try:
    import msvcrt
except ImportError:
    msvcrt = None
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent
UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
GOAL_STATUSES = {'active', 'paused', 'blocked', 'usage_limited', 'budget_limited', 'complete'}
DEFAULT_THRESHOLDS = {'compactions': 3, 'taskTurns': 20, 'logBytes': 50_000_000}


def acquire_lock(stream):
    if fcntl is not None:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    raise RuntimeError('No supported file-locking module is available')


def configured_thresholds():
    """Load install-time thresholds, falling back to the recommended defaults."""
    path = BASE / 'monitor-config.json'
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return DEFAULT_THRESHOLDS.copy()
    values = config.get('thresholds', {})
    result = DEFAULT_THRESHOLDS.copy()
    for key in result:
        value = values.get(key, result[key])
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f'Invalid positive integer threshold: {key}')
        result[key] = value
    return result


def read_goal_modes(path):
    """Read only goal identity/status; never load the user's goal objective."""
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        rows = connection.execute('SELECT thread_id, goal_id, status FROM thread_goals').fetchall()
        if any(status not in GOAL_STATUSES for _, _, status in rows):
            raise ValueError('Unknown goal status; refuse automatic handoff')
        return {sid: {'goalId': goal_id, 'status': status} for sid, goal_id, status in rows}
    finally:
        connection.close()


def update_goal_protection(entry, sid, goal_modes, error=None):
    goal = goal_modes.get(sid, {})
    entry.update(goalMode=bool(goal and goal.get('status') != 'complete'),
                 goalStatus=goal.get('status'), goalProtectionUnknown=bool(error))
    if entry['goalMode'] or error:
        entry['handoffSuppressedReason'] = 'goal_mode' if entry['goalMode'] else 'goal_state_unavailable'
    else:
        entry.pop('handoffSuppressedReason', None)


def configured_owner():
    """Return the thread currently bound to the heartbeat automation, if known."""
    try:
        config = (BASE / 'automation.toml').read_text(encoding='utf-8')
    except OSError:
        return None
    match = re.search(r'(?m)^target_thread_id\s*=\s*[\"\']([^\"\']+)[\"\']\s*$', config)
    owner = match.group(1) if match else None
    return owner if owner and UUID.fullmatch(owner) else None


def successor_authorization_reference():
    """Return a pointer to a human user message; recipients must verify it themselves."""
    try:
        state = json.loads((BASE / 'coordinator-continuation-state.json').read_text(encoding='utf-8'))
        evidence = state.get('authorizationEvidence', {})
    except (OSError, ValueError):
        return None
    required = ('sourceThreadId', 'userTurnId', 'userMessageId', 'inputSha256', 'excerpt')
    if (all(isinstance(evidence.get(key), str) and evidence[key] for key in required)
            and UUID.fullmatch(evidence['sourceThreadId']) and UUID.fullmatch(evidence['userTurnId'])
            and UUID.fullmatch(evidence['userMessageId']) and re.fullmatch(r'[0-9a-f]{64}', evidence['inputSha256'])):
        return {key: evidence[key] for key in required}
    return None


def completed_coordinator_successor(sid):
    """Find a verified coordinator continuation that makes a stale dispatch obsolete."""
    try:
        state = json.loads((BASE / 'coordinator-continuation-state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    mappings = [state, state.get('previousContinuation', {})]
    for mapping in mappings:
        successor = mapping.get('successorThreadId')
        verification = mapping.get('verification') or {}
        if (mapping.get('sourceThreadId') == sid and mapping.get('status') == 'complete'
                and isinstance(successor, str) and UUID.fullmatch(successor)
                and verification):
            return successor
    return None


def now():
    return datetime.now(timezone.utc).isoformat()


def read_metrics(path):
    count = 0
    turns = set()
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.endswith('\n'):
                break
            item = json.loads(line)
            count += item.get('type') == 'compacted'
            payload = item.get('payload') or {}
            if item.get('type') == 'event_msg' and payload.get('type') == 'task_started' and payload.get('turn_id'):
                turns.add(payload['turn_id'])
    return {'count': count, 'taskTurns': len(turns), 'logBytes': path.stat().st_size}


def count_events(path):
    return read_metrics(path)['count']


def trigger_reasons(entry, thresholds=None):
    thresholds = thresholds or configured_thresholds()
    reasons = []
    if entry.get('count', 0) >= thresholds['compactions']:
        reasons.append(f"压缩达到{thresholds['compactions']}次")
    if entry.get('taskTurns', 0) >= thresholds['taskTurns']:
        reasons.append(f"任务达到{thresholds['taskTurns']}轮")
    if entry.get('logBytes', 0) >= thresholds['logBytes']:
        reasons.append(f"日志达到{thresholds['logBytes'] / 1_000_000:g} MB")
    if entry.get('userRequestedHandoff'):
        reasons.append('用户明确要求切换')
    return reasons


def probe_delivery(path, handoff_id=None, sender=None, expected_text=None):
    """Check actual destination inputs, including native cross-thread tool delivery."""
    evidence = None
    turn = None
    finished = set()
    incomplete = False
    malformed = False
    if not handoff_id and expected_text is None:
        raise ValueError('Expected a handoff id or exact message text')
    marker = re.compile(r'handoffId:\s*' + re.escape(handoff_id) + r'(?![\w:-])') if handoff_id else None
    try:
        with path.open(encoding='utf-8') as stream:
            for number, line in enumerate(stream, 1):
                if not line.endswith('\n'):
                    incomplete = True
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    malformed = True
                    continue
                payload = record.get('payload', {})
                if record.get('type') == 'event_msg':
                    if payload.get('type') == 'task_started':
                        turn = payload.get('turn_id')
                    elif payload.get('type') in ('task_complete', 'turn_aborted'):
                        finished.add(payload.get('turn_id'))
                if record.get('type') != 'response_item':
                    continue
                body = None
                origin = None
                transport = None
                if payload.get('type') == 'message' and payload.get('role') == 'user':
                    body = '\n'.join(item.get('text', '') for item in payload.get('content', []) if isinstance(item, dict))
                    transport = 'user_message'
                elif (payload.get('type') == 'function_call_output'
                      and payload.get('namespace') == 'codex_app'
                      and payload.get('name') == 'send_message_to_thread'):
                    # Native cross-thread inputs are tool results, not ordinary user bubbles.
                    output = payload.get('output')
                    match = re.fullmatch(r'\s*<codex_delegation>\s*<source_thread_id>([^<]+)</source_thread_id>\s*<input>(.*)</input>\s*</codex_delegation>\s*', output, re.S) if isinstance(output, str) else None
                    if match and UUID.fullmatch(match.group(1)):
                        origin, body = match.groups()
                        transport = 'codex_delegation'
                matches = body == expected_text if expected_text is not None else body is not None and marker.search(body)
                if not matches or (sender and origin and sender != origin):
                    continue
                evidence = {'path': str(path), 'line': number, 'itemId': payload.get('id'),
                            'deliveredAtUtc': record.get('timestamp'), 'turnId': turn,
                            'transport': transport, 'senderThreadId': origin,
                            'inputSha256': hashlib.sha256(body.encode('utf-8')).hexdigest()}
    except OSError as error:
        return {'result': 'error', 'error': str(error), 'checkedAtUtc': now()}
    if evidence:
        return {'result': 'delivered', 'evidence': evidence,
                'sourceTurnFinished': bool(evidence['turnId'] and evidence['turnId'] in finished), 'checkedAtUtc': now()}
    return {'result': 'unconfirmed' if incomplete or malformed else 'absent',
            'incompleteTail': incomplete, 'malformedRecord': malformed, 'checkedAtUtc': now()}


def refresh_delivery(sid, entry, sessions):
    if sessions is None or not entry.get('handoffId'):
        return
    paths = list(sessions.glob('*/*/*/rollout-*' + sid + '.jsonl'))
    if entry.get('status') in ('dispatching', 'requested', 'delivered'):
        probe = probe_delivery(paths[0], entry['handoffId'], entry.get('senderThreadId')) if len(paths) == 1 else {
            'result': 'unconfirmed', 'error': 'Expected one destination transcript; found ' + str(len(paths)), 'checkedAtUtc': now()}
        entry['deliveryProbe'] = probe
        if probe['result'] == 'delivered':
            entry.update(status='delivered', deliveryVerifiedAtUtc=probe['checkedAtUtc'],
                         visibleMessageEvidence=probe['evidence'], sourceTurnFinished=probe['sourceTurnFinished'])
    attempts = entry.get('recoveryAttempts', [])
    for attempt in attempts:
        if not attempt.get('id') or attempt.get('status') not in ('dispatching', 'requested', 'delivered'):
            continue
        recovery_probe = probe_delivery(paths[0], attempt['id'], entry.get('senderThreadId')) if len(paths) == 1 else {
            'result': 'unconfirmed', 'error': 'Expected one destination transcript; found ' + str(len(paths)), 'checkedAtUtc': now()}
        attempt['deliveryProbe'] = recovery_probe
        if recovery_probe['result'] == 'delivered':
            attempt.update(status='delivered', deliveryVerifiedAtUtc=recovery_probe['checkedAtUtc'],
                           evidence=recovery_probe['evidence'], sourceTurnFinished=recovery_probe['sourceTurnFinished'])
    followups = entry.get('followupAttempts', [])
    for attempt in followups:
        if not attempt.get('id') or attempt.get('status') not in ('dispatching', 'requested', 'delivered'):
            continue
        probe = probe_delivery(paths[0], attempt['id'], entry.get('senderThreadId')) if len(paths) == 1 else {
            'result': 'unconfirmed', 'error': 'Expected one destination transcript; found ' + str(len(paths)), 'checkedAtUtc': now()}
        attempt['deliveryProbe'] = probe
        if probe['result'] == 'delivered':
            attempt.update(status='delivered', deliveryVerifiedAtUtc=probe['checkedAtUtc'],
                           evidence=probe['evidence'], sourceTurnFinished=probe['sourceTurnFinished'])


def notification_note(entry):
    if entry.get('goalMode'):
        return '（目标模式，免通知交接）'
    if entry.get('goalProtectionUnknown'):
        return '（目标状态无法核验，暂不通知交接）'
    if not trigger_reasons(entry):
        return ''
    status = entry.get('status')
    if status in ('dispatching', 'requested') and entry.get('deliveryProbe', {}).get('result') == 'absent' and entry.get('sendAttempt', 0) >= 3:
        return '（目标日志多次未发现原指令，重试已达上限，需人工介入）'
    if status == 'requested':
        return '（通知工具已接受，目标日志送达情况待核验）'
    if status == 'delivered':
        followups = entry.get('followupAttempts', [])
        if followups:
            latest = followups[-1]
            if latest.get('status') == 'requested' and latest.get('deliveryProbe', {}).get('result') == 'absent' and latest.get('sendAttempt', 1) >= 2:
                return '（原指令已核实收到，但交接催办多次未在目标日志确认，需人工介入）'
            if latest.get('status') in ('dispatching', 'requested'):
                return '（原指令已由目标日志核实收到，交接催办已提交，催办送达待核验）'
            if len(followups) >= 3:
                return '（交接催办已达3次上限，仍未完成交接，需人工介入）'
            if latest.get('status') == 'delivered':
                return '（原指令与交接催办均由目标日志核实收到，交接仍待完成）'
        if entry.get('sourceStatus') == 'active':
            return '（目标日志已核实收到指令，目标回合仍在运行；不代表本对话界面显示普通气泡）'
        if entry.get('sourceStatus') == 'idle' or entry.get('sourceTurnFinished'):
            return '（目标日志已核实收到指令，原回合已结束，等待交接恢复）'
        return '（目标日志已核实收到指令，正在核对目标回合状态）'
    if status == 'checkpoint_found' and not entry.get('successorThreadId'):
        return '（交接点已保存，待源对话创建接续）'
    if status in ('checkpoint_found', 'successor_created', 'successor_started', 'handoff_read'):
        return '（交接点已保存，正在接续）'
    if status == 'titles_normalized':
        return '（接续已接收，标题已规范）'
    if status == 'complete':
        return '（交接已完成）'
    return '（通知待发送）'


def threads_from_snapshot(snapshot):
    if 'threads' in snapshot:
        return snapshot.get('pinnedThreads', []) + snapshot['threads']
    for block in snapshot.get('content', []):
        if block.get('type') == 'text':
            try:
                value = json.loads(block['text'])
            except (ValueError, KeyError):
                continue
            if 'threads' in value:
                return threads_from_snapshot(value)
    raise ValueError('Expected a list_threads JSON snapshot')


def find_checkpoint(cwd, sid, handoff_id=None, reset_at=None):
    if not cwd:
        return None
    root = Path(cwd)
    if not root.is_absolute():
        return None
    paths = list(root.glob('.codex/work/*/continuation-state.json')) + list(root.glob('.codex/work/*/handoff/continuation-state.json'))
    for checkpoint in paths:
        try:
            data = json.loads(checkpoint.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        successor = data.get('successorThreadId')
        if data.get('sourceThreadId') == sid:
            if reset_at and (not handoff_id or data.get('handoffId') != handoff_id):
                continue  # An explicit reset must not resurrect an old checkpoint.
            return checkpoint, data
    return None


def expose_pending(output, sid, entry, sessions=None):
    """Refresh checkpoint evidence before returning a pending handoff."""
    if entry.get('goalMode') or entry.get('goalProtectionUnknown'):
        output['protected'].append({'threadId': sid, 'reason': entry['handoffSuppressedReason']})
        return
    refresh_delivery(sid, entry, sessions)
    found = find_checkpoint(entry.get('cwd'), sid, entry.get('handoffId'), entry.get('notificationResetAtUtc'))
    if found:
        checkpoint, data = found
        successor = data.get('successorThreadId')
        entry.update(checkpoint=str(checkpoint), checkpointSuccessorThreadId=successor)
        if isinstance(successor, str) and UUID.fullmatch(successor):
            entry['successorThreadId'] = successor
        if entry.get('status') in ('requested', 'delivered'):
            entry.update(status='checkpoint_found', checkpointFoundAtUtc=data.get('createdAtUtc') or now(),
                         deliveryEvidence='inferred_from_source_checkpoint')
    next_action = {
        'dispatching': 'verify_message_or_wait_for_source_turn',
        'requested': 'verify_message_then_checkpoint',
        'delivered': 'wait_for_checkpoint',
        'checkpoint_found': ('wait_for_source_turn' if entry.get('sourceStatus') != 'idle' else
                             'recover_existing_successor' if entry.get('successorThreadId') else 'request_source_create_successor'),
        'successor_created': 'verify_successor_started_and_handoff_read' if entry.get('sourceStatus') == 'idle' else 'wait_for_source_turn',
        'successor_started': 'verify_handoff_read' if entry.get('sourceStatus') == 'idle' else 'wait_for_source_turn',
        'handoff_read': 'normalize_titles' if entry.get('sourceStatus') == 'idle' else 'wait_for_source_turn',
        'titles_normalized': 'complete',
    }.get(entry.get('status'), 'reconcile_evidence')
    if entry.get('status') in ('dispatching', 'requested'):
        probe = entry.get('deliveryProbe', {})
        accepted = entry.get('toolAcceptedAtUtc') or entry.get('requestedAtUtc')
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(accepted)).total_seconds() if accepted else 0
        if probe.get('result') == 'absent' and age >= 60 and entry.get('sendAttempt', 0) < 3:
            next_action = 'retry_initial_delivery'
        elif probe.get('result') == 'absent' and entry.get('sendAttempt', 0) >= 3:
            next_action = 'delivery_retry_limit_reached'
    if entry.get('status') == 'delivered' and not found and entry.get('sourceStatus') == 'active':
        followups = entry.get('followupAttempts', [])
        last_followup = followups[-1] if followups else None
        if last_followup and last_followup.get('status') in ('dispatching', 'requested'):
            probe = last_followup.get('deliveryProbe', {})
            accepted = last_followup.get('toolAcceptedAtUtc')
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(accepted)).total_seconds() if accepted else 0
            if (last_followup.get('status') == 'requested' and probe.get('result') == 'absent'
                    and age >= 60 and last_followup.get('sendAttempt', 1) >= 2):
                next_action = 'followup_delivery_failed'
            else:
                next_action = ('retry_followup_delivery' if last_followup.get('status') == 'requested'
                               and probe.get('result') == 'absent' and age >= 60
                               and last_followup.get('sendAttempt', 1) < 2 else 'verify_followup_delivery')
        elif last_followup and last_followup.get('status') == 'delivered':
            if len(followups) >= 3:
                next_action = 'followup_limit_reached'
            elif last_followup.get('sourceTurnFinished'):
                delivered_at = last_followup.get('evidence', {}).get('deliveredAtUtc')
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(delivered_at.replace('Z', '+00:00'))).total_seconds() if delivered_at else 0
                next_action = 'nudge_source_handoff' if age >= 1800 else 'wait_for_source_turn'
            else:
                next_action = 'wait_for_source_turn'
        elif len(followups) >= 3:
            next_action = 'followup_limit_reached'
        else:
            delivered_at = entry.get('deliveryProbe', {}).get('evidence', {}).get('deliveredAtUtc')
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(delivered_at.replace('Z', '+00:00'))).total_seconds() if delivered_at else 0
            next_action = 'nudge_source_handoff' if age >= 600 else 'wait_for_source_turn'
    elif entry.get('status') == 'delivered' and not found and entry.get('sourceStatus') == 'idle':
        attempts = entry.get('recoveryAttempts', [])
        last = attempts[-1] if attempts else None
        if not last:
            next_action = 'request_source_handoff_recovery'
        elif last.get('status') == 'dispatching':
            next_action = 'verify_recovery_delivery'
        elif last.get('status') == 'requested':
            probe = last.get('deliveryProbe', {})
            accepted = last.get('toolAcceptedAtUtc')
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(accepted)).total_seconds() if accepted else 0
            next_action = ('retry_source_handoff_recovery' if probe.get('result') == 'absent' and age >= 60
                           and len(attempts) < 2 else 'verify_recovery_delivery')
        elif last.get('status') == 'delivered':
            next_action = 'request_source_handoff_recovery' if len(attempts) < 2 else 'recovery_limit_reached'
        else:
            next_action = 'verify_recovery_delivery'
    output['pending'].append(dict(entry, threadId=sid, nextAction=next_action))


def scan(snapshot, state, sessions, goal_modes=None, goal_error=None):
    output = {'scanned': [], 'eligible': [], 'pending': [], 'protected': [], 'warnings': []}
    goal_modes = goal_modes or {}
    if goal_error:
        output['warnings'].append({'error': 'Goal mode verification unavailable: ' + goal_error})
    owner = configured_owner()
    seen = set()
    threads = threads_from_snapshot(snapshot)
    live = {thread.get('id'): thread for thread in threads if thread.get('hostId') == 'local' and thread.get('kind') == 'codex'}
    for sid, entry in state.get('threads', {}).items():
        update_goal_protection(entry, sid, goal_modes, goal_error)
        if entry.get('status') in ('dispatching', 'requested', 'delivered'):
            successor = completed_coordinator_successor(sid)
            if successor:
                entry.update(status='superseded', supersededByThreadId=successor,
                             supersededAtUtc=now(), supersededReason='A verified coordinator continuation is already complete')
        source = live.get(sid, {})
        status = source.get('status', 'unknown')
        entry['sourceStatus'] = status.get('type', 'unknown') if isinstance(status, dict) else status
    for thread in threads:
        sid = thread.get('id', '')
        status = thread.get('status')
        flags = (status.get('activeFlags', []) if isinstance(status, dict) else []) or thread.get('activeFlags', [])
        status = status.get('type') if isinstance(status, dict) else status
        if sid in seen or thread.get('kind') != 'codex' or thread.get('hostId') != 'local' or status != 'active' or flags:
            continue
        seen.add(sid)
        if not UUID.fullmatch(sid):
            output['warnings'].append({'threadId': sid, 'error': 'Invalid thread id'})
            continue
        try:
            paths = list(sessions.glob('*/*/*/rollout-*' + sid + '.jsonl'))
            if len(paths) != 1:
                raise ValueError('Expected one transcript; found ' + str(len(paths)))
            metrics = read_metrics(paths[0])
            count = metrics['count']
            entry = state.setdefault('threads', {}).setdefault(sid, {})
            update_goal_protection(entry, sid, goal_modes, goal_error)
            is_monitor_owner = sid == owner
            entry.update(**metrics, triggerReasons=trigger_reasons(metrics), observedAt=now(), cwd=thread.get('cwd'), title=thread.get('title'), isMonitorOwner=is_monitor_owner, sourceStatus=status)
            output['scanned'].append({'threadId': sid, 'title': entry['title'], **metrics, 'triggerReasons': entry['triggerReasons'], 'isMonitorOwner': is_monitor_owner})
            if entry.get('goalMode') or entry.get('goalProtectionUnknown'):
                output['protected'].append({'threadId': sid, 'reason': entry['handoffSuppressedReason']})
                continue
            if entry.get('status') == 'complete':
                continue
            if entry.get('status') in ('dispatching', 'requested', 'delivered', 'checkpoint_found', 'successor_created', 'successor_started', 'handoff_read', 'titles_normalized'):
                expose_pending(output, sid, entry, sessions)
                continue
            cwd = Path(thread['cwd']) if thread.get('cwd') else None
            found = find_checkpoint(cwd, sid, entry.get('handoffId'), entry.get('notificationResetAtUtc'))
            if found:
                checkpoint, data = found
                entry.update(status='checkpoint_found', checkpoint=str(checkpoint), checkpointFoundAtUtc=data.get('createdAtUtc') or now(),
                             checkpointSuccessorThreadId=data.get('successorThreadId'))
                if isinstance(data.get('successorThreadId'), str) and UUID.fullmatch(data['successorThreadId']):
                    entry['successorThreadId'] = data['successorThreadId']
                expose_pending(output, sid, entry, sessions)
            elif trigger_reasons(entry):
                output['eligible'].append(dict(entry, threadId=sid))
        except (ValueError, OSError, KeyError) as error:
            output['warnings'].append({'threadId': sid, 'error': str(error)})
    pending_ids = {entry['threadId'] for entry in output['pending'] + output['protected']}
    for sid, entry in state.get('threads', {}).items():
        # A source normally becomes idle after handing off; keep verifying its request.
        if entry.get('status') in ('dispatching', 'requested', 'delivered', 'checkpoint_found', 'successor_created', 'successor_started', 'handoff_read', 'titles_normalized') and sid not in pending_ids:
            expose_pending(output, sid, entry, sessions)
    for item in output['scanned']:
        item['notificationNote'] = notification_note(state['threads'][item['threadId']])
    state['lastScanAt'] = now()
    return output


def transition(state, action, sid, successor=None, evidence=None):
    if not UUID.fullmatch(sid):
        raise ValueError('Invalid thread id')
    entry = state.setdefault('threads', {}).get(sid)
    if not entry:
        raise ValueError('Scan the live thread before requesting a handoff')
    protected_actions = {'claim', 'retry', 'recover', 'recovery_sent', 'nudge', 'nudge_sent', 'retry_followup', 'successor_created', 'successor_started', 'handoff_read', 'titles_normalized', 'complete'}
    if action in protected_actions and (entry.get('goalMode') or entry.get('goalProtectionUnknown')):
        reason = 'goal_mode' if entry.get('goalMode') else 'goal_state_unavailable'
        if action == 'claim':
            return {'claimed': False, 'threadId': sid, 'status': entry.get('status'), 'reason': reason}
        raise ValueError('Automatic handoff suppressed: ' + reason)
    if action == 'reset':
        retained = {key: entry[key] for key in ('count', 'taskTurns', 'logBytes', 'triggerReasons', 'userRequestedHandoff', 'observedAt', 'cwd', 'title', 'isMonitorOwner', 'sourceStatus', 'goalMode', 'goalStatus', 'goalProtectionUnknown', 'handoffSuppressedReason') if key in entry}
        retained.update(notificationResetAtUtc=now(), resetGeneration=uuid.uuid4().hex[:12])
        entry.clear()
        entry.update(retained)
        return {'threadId': sid, 'status': 'not_sent', 'count': entry.get('count'), 'resetGeneration': entry['resetGeneration']}
    if action == 'claim':
        allowed = bool(trigger_reasons(entry)) and not entry.get('status')
        if allowed:
            handoff_id = sid + ':' + str(entry.get('count', 0))
            if entry.get('resetGeneration'):
                handoff_id += ':r' + entry['resetGeneration']
            entry.update(status='dispatching', handoffId=handoff_id, requestedAtUtc=now(), senderThreadId=configured_owner(), handoffKind='coordinator' if entry.get('isMonitorOwner') else 'project', sendAttempt=0)
        return {'claimed': allowed, 'threadId': sid, 'status': entry.get('status'),
                'handoffId': entry.get('handoffId') if allowed else None}
    if action == 'recover':
        attempts = entry.setdefault('recoveryAttempts', [])
        found = find_checkpoint(entry.get('cwd'), sid, entry.get('handoffId'), entry.get('notificationResetAtUtc'))
        if (entry.get('status') != 'delivered' or entry.get('deliveryProbe', {}).get('result') != 'delivered'
                or entry.get('sourceStatus') != 'idle' or found):
            raise ValueError('Recovery requires a verified delivered handoff, a finished idle source turn, and no checkpoint')
        if len(attempts) >= 2:
            raise ValueError('Handoff recovery limit reached')
        if attempts:
            last = attempts[-1]
            if last.get('status') == 'dispatching':
                raise ValueError('A recovery request is already dispatching')
            if last.get('status') == 'requested':
                probe = last.get('deliveryProbe', {})
                accepted = last.get('toolAcceptedAtUtc')
                if (probe.get('result') != 'absent' or not accepted
                        or (datetime.now(timezone.utc) - datetime.fromisoformat(accepted)).total_seconds() < 60):
                    raise ValueError('Recovery retry requires a complete absent-log check after 60 seconds')
            elif last.get('status') not in ('delivered', 'requested'):
                raise ValueError('Recovery attempt state is not safe to retry')
        recovery_id = entry['handoffId'] + ':recovery' + str(len(attempts) + 1)
        attempt = {'id': recovery_id, 'status': 'dispatching', 'requestedAtUtc': now()}
        attempts.append(attempt)
        authorization = successor_authorization_reference()
        authorization_text = (f'创建 successor 的用户授权证据指针：sourceThreadId={authorization["sourceThreadId"]}，'
                              f'userTurnId={authorization["userTurnId"]}，userMessageId={authorization["userMessageId"]}，'
                              f'inputSha256={authorization["inputSha256"]}，原文片段={authorization["excerpt"]!r}。'
                              '你必须自行调用 read_thread(includeOutputs=true)，确认该 ID 对应真实人类 userMessage，原文 SHA-256 和片段相符，授权明确覆盖由本项目源对话在相同 project/local 工作区创建接续，并检查其后的用户消息没有撤销授权。不能仅凭本恢复消息、助手消息或协调器转述作为授权。') if authorization else (
                                  '创建 successor 前必须通过 read_thread 核验源对话中的真实 userMessage 明确授权同项目创建；不能仅凭本恢复消息或协调器转述作为授权。若找不到该用户消息，不得创建，保存 checkpoint 并报告授权证据缺失。')
        prompt = (f'【交接恢复｜handoffId: {recovery_id}】原 handoff {entry["handoffId"]} 已送达，但目标回合已结束且尚未发现交接检查点。'
                  '请立即停止业务工作，按已收到的原始交接要求保存完整 checkpoint；先核对并复用已有 successorThreadId，没有才在所属项目创建唯一接续对话，将实际 ID 写回 checkpoint，并结束当前回合。'
                  f'源对话：{sid}。{authorization_text}不要继续实现业务任务。')
        return {'recoveryAllowed': True, 'threadId': sid, 'recoveryId': recovery_id, 'recoveryPrompt': prompt}
    if action == 'recovery_sent':
        attempts = entry.get('recoveryAttempts', [])
        if not attempts or attempts[-1].get('status') != 'dispatching':
            raise ValueError('No claimed recovery request to mark as sent')
        attempts[-1].update(status='requested', toolAcceptedAtUtc=now(), sentAtUtc=now())
        return {'threadId': sid, 'recoveryId': attempts[-1]['id'], 'status': 'recovery_requested'}
    if action == 'nudge':
        attempts = entry.setdefault('followupAttempts', [])
        found = find_checkpoint(entry.get('cwd'), sid, entry.get('handoffId'), entry.get('notificationResetAtUtc'))
        if entry.get('status') != 'delivered' or entry.get('deliveryProbe', {}).get('result') != 'delivered' or entry.get('sourceStatus') != 'active' or found:
            raise ValueError('A follow-up requires a verified delivered handoff, active source, and no checkpoint')
        if len(attempts) >= 3:
            raise ValueError('Handoff follow-up limit reached; keep pending for idle recovery')
        if attempts:
            last = attempts[-1]
            if last.get('status') != 'delivered' or not last.get('sourceTurnFinished'):
                raise ValueError('The previous follow-up must be verified and its receiving turn must finish first')
            delivered_at = last.get('evidence', {}).get('deliveredAtUtc')
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(delivered_at.replace('Z', '+00:00'))).total_seconds() if delivered_at else 0
            if age < 1800:
                raise ValueError('Wait 30 minutes after the last completed follow-up before another nudge')
        else:
            delivered_at = entry.get('deliveryProbe', {}).get('evidence', {}).get('deliveredAtUtc')
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(delivered_at.replace('Z', '+00:00'))).total_seconds() if delivered_at else 0
            if age < 600:
                raise ValueError('Wait 10 minutes after verified delivery before the first follow-up')
        followup_id = entry['handoffId'] + ':followup' + str(len(attempts) + 1)
        authorization = successor_authorization_reference()
        authorization_text = (f'接续创建授权证据：sourceThreadId={authorization["sourceThreadId"]}，userTurnId={authorization["userTurnId"]}，userMessageId={authorization["userMessageId"]}，inputSha256={authorization["inputSha256"]}，原文片段={authorization["excerpt"]!r}。创建前必须自行读取并核实这条真实 userMessage 及后续未撤销授权。' if authorization else '创建接续前必须自行核验原始用户 userMessage 明确授权同项目创建；无法核验时保存检查点并说明缺口。')
        prompt = (f'【交接催办｜handoffId: {followup_id}】原交接指令 {entry["handoffId"]} 已在目标日志核实送达，但尚未发现项目检查点。请立即停止新增业务工作，保存完整 continuation-state.json，记录最后完成项及全部未完成项；复用已有 successorThreadId，若无则在本项目创建唯一接续对话，写回真实 ID，并将源/新对话标题规范为 OLD·/NEW·同一基名后结束当前回合。不要等待协调器继续催办，也不要继续业务实现。{authorization_text}')
        attempts.append({'id': followup_id, 'status': 'dispatching', 'requestedAtUtc': now(), 'sendAttempt': 0, 'prompt': prompt})
        return {'followupAllowed': True, 'threadId': sid, 'followupId': followup_id, 'followupPrompt': prompt}
    if action == 'nudge_sent':
        attempts = entry.get('followupAttempts', [])
        if not attempts or attempts[-1].get('status') != 'dispatching':
            raise ValueError('No claimed follow-up request to mark as sent')
        attempts[-1].update(status='requested', toolAcceptedAtUtc=now(), sentAtUtc=now(), sendAttempt=attempts[-1].get('sendAttempt', 0) + 1)
        return {'threadId': sid, 'followupId': attempts[-1]['id'], 'status': 'followup_requested'}
    if action == 'retry_followup':
        attempts = entry.get('followupAttempts', [])
        if not attempts or attempts[-1].get('status') != 'requested':
            raise ValueError('No accepted follow-up request is pending verification')
        last = attempts[-1]
        probe = last.get('deliveryProbe', {})
        accepted = last.get('toolAcceptedAtUtc')
        if (probe.get('result') != 'absent' or not accepted
                or (datetime.now(timezone.utc) - datetime.fromisoformat(accepted)).total_seconds() < 60
                or last.get('sendAttempt', 1) >= 2):
            raise ValueError('Follow-up retry requires a complete absent-log check after 60 seconds and fewer than two accepted sends')
        last.update(status='dispatching', retryRequestedAtUtc=now())
        return {'retryAllowed': True, 'threadId': sid, 'followupId': last['id'], 'followupPrompt': last.get('prompt')}
    if action == 'sent':
        if entry.get('status') != 'dispatching':
            raise ValueError('No claimed request to mark as sent')
        # Tool acceptance only means queued/accepted; it does not prove visibility.
        entry.update(status='requested', toolAcceptedAtUtc=now(), sentAt=now(), sendAttempt=entry.get('sendAttempt', 0) + 1)
    elif action == 'retry':
        if entry.get('status') not in ('requested', 'dispatching') or not evidence:
            raise ValueError('Retry requires a pending message plus evidence that the complete destination log is absent')
        probe = entry.get('deliveryProbe', {})
        if entry.get('sourceStatus') not in ('active', 'idle') or probe.get('result') != 'absent':
            raise ValueError('Retry requires a live local source and a complete destination-log check proving absence')
        accepted = entry.get('toolAcceptedAtUtc') or entry.get('requestedAtUtc')
        if not accepted or (datetime.now(timezone.utc) - datetime.fromisoformat(accepted)).total_seconds() < 60:
            raise ValueError('Wait at least 60 seconds after tool acceptance before an absence retry')
        if entry.get('sendAttempt', 0) >= 3:
            raise ValueError('Retry limit reached; keep pending and report for recovery')
        entry.update(status='dispatching', retryRequestedAtUtc=now(), retryEvidence=evidence,
                     retryCount=entry.get('retryCount', 0) + 1)
    elif action == 'delivered':
        if entry.get('status') not in ('requested', 'dispatching') or not evidence:
            raise ValueError('Visible-message evidence is required')
        entry.update(status='delivered', deliveryVerifiedAtUtc=now(), visibleMessageEvidence=evidence)
    elif action == 'successor_created':
        if entry.get('status') != 'checkpoint_found' or not successor or not UUID.fullmatch(successor):
            raise ValueError('A checkpoint and created successor id are required')
        if entry.get('handoffKind') != 'coordinator' and entry.get('sourceStatus') != 'idle':
            raise ValueError('Project successor requires the latest source status to be idle')
        if entry.get('successorThreadId') and successor != entry.get('successorThreadId'):
            raise ValueError('Successor id must match the checkpoint')
        entry.update(status='successor_created', successorThreadId=successor, successorCreatedAtUtc=now())
    elif action == 'successor_started':
        if entry.get('status') != 'successor_created' or successor != entry.get('successorThreadId') or entry.get('sourceStatus') != 'idle' or not evidence:
            raise ValueError('Started-event evidence for the checkpoint successor is required')
        entry.update(status='successor_started', successorStartedAtUtc=now(), successorStartedEvidence=evidence)
    elif action == 'handoff_read':
        if entry.get('status') != 'successor_started' or successor != entry.get('successorThreadId') or entry.get('sourceStatus') != 'idle' or not evidence:
            raise ValueError('Handoff-read evidence from the successor is required')
        entry.update(status='handoff_read', handoffReadAtUtc=now(), handoffReadEvidence=evidence)
    elif action == 'titles_normalized':
        if entry.get('status') != 'handoff_read' or successor != entry.get('successorThreadId') or entry.get('sourceStatus') != 'idle' or not evidence:
            raise ValueError('Title normalization requires a read handoff, matching successor, and idle source')
        try:
            titles = json.loads(evidence)
        except ValueError as error:
            raise ValueError('Title evidence must be JSON from a verified thread listing') from error
        source_title, successor_title = titles.get('sourceTitle'), titles.get('successorTitle')
        if not isinstance(source_title, str) or not isinstance(successor_title, str):
            raise ValueError('Both actual thread titles are required')
        old_prefix, new_prefix = 'OLD·', 'NEW·'
        old_base = source_title[len(old_prefix):] if source_title.startswith(old_prefix) else ''
        new_base = successor_title[len(new_prefix):] if successor_title.startswith(new_prefix) else ''
        nested = ('OLD·', 'NEW·', '续接·')
        if not old_base or old_base != new_base or old_base.startswith(nested):
            raise ValueError('Titles must be OLD·<same base> and NEW·<same base>, without duplicate prefixes')
        entry.update(status='titles_normalized', titlesNormalizedAtUtc=now(), titlesEvidence=titles)
    elif action == 'complete':
        if not successor or not UUID.fullmatch(successor) or sid == successor:
            raise ValueError('A distinct verified successor id is required')
        if successor != entry.get('successorThreadId') or entry.get('status') != 'titles_normalized':
            raise ValueError('Cannot complete before the checkpoint successor has read handoff and both titles are verified')
        entry.update(status='complete', completeAtUtc=now())
    return {'threadId': sid, 'status': entry['status']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['scan', 'verify_input', 'reset', 'claim', 'sent', 'retry', 'delivered', 'recover', 'recovery_sent', 'nudge', 'nudge_sent', 'retry_followup', 'successor_created', 'successor_started', 'handoff_read', 'titles_normalized', 'complete'])
    parser.add_argument('thread_id', nargs='?')
    parser.add_argument('successor_id', nargs='?')
    args = parser.parse_args()
    if args.action == 'verify_input':
        if not args.thread_id or not UUID.fullmatch(args.thread_id):
            parser.error('verify_input requires a target thread id')
        value = json.load(sys.stdin)
        if not isinstance(value.get('text'), str) or not UUID.fullmatch(value.get('senderThreadId', '')):
            parser.error('stdin must contain exact text and senderThreadId')
        sessions = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
        paths = list(sessions.glob('*/*/*/rollout-*' + args.thread_id + '.jsonl'))
        result = probe_delivery(paths[0], sender=value['senderThreadId'], expected_text=value['text']) if len(paths) == 1 else {'result': 'unconfirmed', 'error': 'Expected one destination transcript'}
        print(json.dumps(result, ensure_ascii=False))
        return
    lock_path = BASE / 'context-handoff.lock'
    state_path = BASE / 'context-handoff-state.json'
    with lock_path.open('a+b') as lock:
        try:
            acquire_lock(lock)
        except BlockingIOError:
            print(json.dumps({'busy': True}))
            return
        except OSError as error:
            if getattr(error, 'errno', None) in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK):
                print(json.dumps({'busy': True}))
                return
            raise
        state = json.loads(state_path.read_text()) if state_path.exists() else {'version': 1, 'threads': {}}
        goal_modes, goal_error = {}, None
        try:
            codex_root = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
            goal_modes = read_goal_modes(codex_root / 'goals_1.sqlite')
        except (OSError, sqlite3.Error, ValueError) as error:
            goal_error = str(error)
        if args.action == 'scan':
            sessions = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
            result = scan(json.load(sys.stdin), state, sessions, goal_modes, goal_error)
        else:
            if args.thread_id in state.get('threads', {}):
                update_goal_protection(state['threads'][args.thread_id], args.thread_id, goal_modes, goal_error)
            evidence = sys.stdin.read().strip() if not sys.stdin.isatty() else None
            result = transition(state, args.action, args.thread_id or '', args.successor_id, evidence)
        temp = state_path.with_suffix('.tmp')
        descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temp, state_path)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

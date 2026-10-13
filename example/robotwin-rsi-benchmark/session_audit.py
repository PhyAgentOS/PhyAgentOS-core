import hashlib
import json
from pathlib import Path


def records_and_calls(path):
    records = [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    calls = {}
    for record in records:
        for call in record.get('tool_calls') or []:
            function = call.get('function') or {}
            arguments = function.get('arguments') or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            calls[call['id']] = {'name': function.get('name'), 'arguments': arguments,
                                 'assistant_text': record.get('content') or ''}
    for record in records:
        if record.get('role') == 'tool' and record.get('tool_call_id') in calls:
            call = calls[record['tool_call_id']]
            content = record.get('content') or ''
            try:
                call['output'] = json.loads(content)
            except (ValueError, TypeError):
                call['output'] = content
    return records, list(calls.values())


def outputs_of(call):
    value = call.get('output') or {}
    if not isinstance(value, dict):
        return {}
    return value.get('data', {}).get('response', {}).get('result', {}).get('outputs', {})


def checkpoint_evidence(path):
    records, calls = records_and_calls(path)
    facts = []
    last_checkpoint = {}
    visuals = []
    for index, call in enumerate(calls, 1):
        tool_id = call['arguments'].get('tool_id', '')
        output = outputs_of(call)
        if tool_id.startswith('vla.get_status') and output.get('checkpoint'):
            last_checkpoint = output['checkpoint']
        if call['name'] == 'image':
            visuals.append({'caption': str(call.get('output') or '')[:300]})
        if tool_id != 'vla.decide':
            continue
        arguments = call['arguments'].get('arguments') or {}
        closed = output.get('checkpoint_closed') or {}
        details = last_checkpoint.get('details') or {}
        facts.append({
            'source_tool_index': index, 'step': closed.get('step_in_episode', last_checkpoint.get('step_in_episode')),
            'trigger': closed.get('trigger', last_checkpoint.get('trigger')),
            'reason': closed.get('reason', last_checkpoint.get('reason')),
            'mode': arguments.get('mode'), 'accepted': output.get('accepted'),
            'steps': arguments.get('steps'), 'delta_joints': arguments.get('delta_joints'),
            'displacement': details.get('displacement', details.get('displacement_m')),
            'threshold': details.get('threshold', details.get('threshold_m')),
            'counters': last_checkpoint.get('counters') or {},
            'assistant_reasoning': str(call['assistant_text'])[:420],
        })
    priority = sorted(facts, key=lambda fact: (
        fact['mode'] == 'edit', fact['trigger'] == 'C2' or fact['reason'] == 'no_progress',
        fact['source_tool_index']), reverse=True)
    selected = sorted(priority[:2], key=lambda fact: fact['source_tool_index'])
    rationales = [{'assistant_reasoning': fact['assistant_reasoning']} for fact in selected if fact['assistant_reasoning']]
    return {'evidence': selected, 'visuals': visuals[-1:], 'assistant_reasoning': rationales[:1],
            'total_decisions': len(facts)}


def audit_session(path, carrier):
    records, calls = records_and_calls(path)
    activations = [call.get('output') for call in calls if call['name'] == 'activate_skill']
    delivered = any(isinstance(value, dict) and carrier in value.get('skill', '') for value in activations) if carrier else None
    decisions = []
    statuses = []
    for call in calls:
        arguments = call['arguments']
        inner = arguments.get('arguments') or {}
        tool_id = arguments.get('tool_id')
        output = outputs_of(call)
        if tool_id == 'vla.decide':
            closed = output.get('checkpoint_closed') or {}
            decisions.append({'arguments': inner, 'accepted': output.get('accepted'),
                              'checkpoint_closed': closed, 'segment': output.get('segment'),
                              'assistant_text': call['assistant_text']})
        if tool_id and tool_id.startswith('vla.get_status'):
            statuses.append(output)
    assistant = '\n'.join(str(record.get('content') or '') for record in records if record.get('role') == 'assistant')
    final_counters = {}
    for status in statuses:
        counters = (status.get('checkpoint') or {}).get('counters') or status.get('counters')
        if counters:
            final_counters = counters
    activation_skills = [value.get('skill', '') for value in activations if isinstance(value, dict)]
    return {
        'carrier_sha256': hashlib.sha256(carrier.encode()).hexdigest() if carrier else None,
        'carrier_chars': len(carrier), 'carrier_approx_tokens': len(carrier) // 4,
        'carrier_delivered_verbatim': delivered, 'activation_count': len(activations),
        'unexpected_carrier_in_none': not carrier and any('ACTIVE EXPERIENCE CARRIER:' in skill for skill in activation_skills),
        'decision_count': len(decisions),
        'edit_decisions': sum(decision['arguments'].get('mode') == 'edit' for decision in decisions),
        'accepted_edit_decisions': sum(decision['arguments'].get('mode') == 'edit' and decision['accepted'] is True
                                       for decision in decisions),
        'decisions': decisions, 'final_counters': final_counters,
        'candidate_observation_count': sum('policy_next_action' in json.dumps(status) for status in statuses),
        'assistant_evidence_mentions': {term: assistant.casefold().count(term)
                                        for term in ('lesson', 'e1', 'prior', 'no_progress', 'evidence')},
        'assistant_text': assistant,
        'interpretation': 'Delivery and mentions are process evidence, not proof of causal contribution.',
    }

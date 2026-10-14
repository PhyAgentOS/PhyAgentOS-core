import json

from jsonschema import validate


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate JSON key: ' + name)
        result[name] = value
    return result


def reject_constant(value):
    raise ValueError('Non-finite JSON number: ' + value)


def decision_object(content, finish_reason, schema):
    if finish_reason != 'stop':
        raise ValueError('Expected complete response with finish_reason=stop, got ' + str(finish_reason))
    if not isinstance(content, str) or not content.strip():
        raise ValueError('Decision content is empty')
    text = content.strip()
    if text.startswith('```') and text.endswith('```'):
        lines = text.splitlines()
        if lines[0].strip().lower() not in {'```', '```json'}:
            raise ValueError('Unsupported decision code fence')
        text = '\n'.join(lines[1:-1])
    decision = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    validate(decision, schema['oneOf'][0])
    return decision


def decision_template(packet):
    checkpoint = packet['checkpoint']
    return {'kind': 'proposal', 'proposal': {
        'mode': '<student or edit; select using current evidence>',
        'steps': '<integer: student 1..10, edit 1..5>',
        'reason': '<concise current-evidence-based reason>',
        'assessment': {
            'checkpoint_id': checkpoint['checkpoint_id'],
            'observed_step': packet['episode']['steps'],
            'task_progress': {'verified_completed': [], 'currently_attempting': '<current subgoal>',
                'remaining': []},
            'current_subgoal': '<current subgoal>',
            'execution_status': '<not_started ONLY at step 0; otherwise progressing, failed, uncertain or recovered>',
            'execution_evidence': '<evidence from actual previous execution>',
            'expected_next_intent': '<next task-relevant intent>',
            'predicted_next_intent': '<intent inferred from actual pending candidate>',
            'intent_status': '<aligned, misaligned or uncertain>',
            'intent_evidence': '<current image and candidate evidence>',
            'lesson_applicability': '<specific conditional clause and evidence, or none>'}}}


def validation_feedback(error):
    path = '$' + ''.join('[' + str(part) + ']' for part in getattr(error, 'absolute_path', []))
    return {'path': path, 'error': getattr(error, 'message', str(error))[:1600],
        'instruction': 'Repair the previous JSON using the same images and pending candidate. '
            'No physical action has been accepted from that invalid response. Return ONLY kind and proposal. '
            'Do not echo type=json_object, schemas, inputs or additional fields. '
            'Supply missing judgments yourself from the evidence; never default to student just to pass validation. '
            'All action, safety, scope and evidence requirements remain unchanged.'}


def reflection_object(content, finish_reason):
    if finish_reason == 'length':
        raise ValueError('Reflection was truncated before a complete JSON response')
    if not isinstance(content, str) or not content.strip():
        raise ValueError('Reflection content is empty')
    text = content.strip()
    if text.startswith('```') and text.endswith('```'):
        lines = text.splitlines()
        if lines[0].strip().lower() not in {'```', '```json'}:
            raise ValueError('Unsupported reflection code fence')
        text = '\n'.join(lines[1:-1]).strip()
    report = json.loads(text)
    required = {'task_steps', 'failure_step_or_unknown', 'observed_evidence',
                'conditional_recovery', 'applicability', 'uncertainty'}
    if not isinstance(report, dict) or not required.issubset(report) or not report['observed_evidence']:
        raise ValueError('Reflection schema is missing required evidence')
    return report

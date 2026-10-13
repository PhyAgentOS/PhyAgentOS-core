import base64
import copy
import hashlib
import json
from pathlib import Path


VERSION = 'outcome_intent_images_v1'
REQUIRED_TEXT = ('current_subgoal', 'execution_evidence', 'expected_next_intent',
                 'predicted_next_intent', 'intent_evidence', 'lesson_applicability')


def validate_assessment(arguments, status):
    checkpoint = status.get('checkpoint') or {}
    step = (status.get('episode') or {}).get('steps')
    if not checkpoint.get('checkpoint_id') or (status.get('candidate') or {}).get('freshness') != 'pending':
        raise ValueError('Decision requires an open checkpoint and an unexecuted pending candidate')
    mode = arguments.get('mode')
    if mode not in {'student', 'edit'}:
        raise ValueError('This joint-space supervisor permits only student or edit decisions')
    if type(arguments.get('steps')) is not int or not 1 <= arguments['steps'] <= 10:
        raise ValueError('Decision must execute a bounded segment of 1..10 native steps')
    assessment = arguments.get('assessment')
    if not isinstance(assessment, dict):
        raise ValueError('Structured outcome and next-intent assessment is required')
    if assessment.get('checkpoint_id') != checkpoint['checkpoint_id']:
        raise ValueError('Assessment belongs to another checkpoint')
    if type(assessment.get('observed_step')) is not int or assessment['observed_step'] != step:
        raise ValueError('Assessment belongs to another episode step')
    progress = assessment.get('task_progress')
    if not isinstance(progress, dict):
        raise ValueError('task_progress is required')
    for key in ('verified_completed', 'remaining'):
        values = progress.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError('task_progress.' + key + ' must be a list of nonempty strings')
    if not isinstance(progress.get('currently_attempting'), str) or not progress['currently_attempting'].strip():
        raise ValueError('task_progress.currently_attempting is required')
    for key in REQUIRED_TEXT:
        if not isinstance(assessment.get(key), str) or not assessment[key].strip():
            raise ValueError('Assessment requires nonempty ' + key)
    execution = assessment.get('execution_status')
    intent = assessment.get('intent_status')
    if execution not in {'not_started', 'progressing', 'failed', 'uncertain', 'recovered'}:
        raise ValueError('Unknown execution_status')
    if intent not in {'aligned', 'misaligned', 'uncertain'}:
        raise ValueError('Unknown intent_status')
    if (step == 0) != (execution == 'not_started'):
        raise ValueError('not_started is valid only at episode step zero')
    if not isinstance(arguments.get('reason'), str) or not arguments['reason'].strip():
        raise ValueError('A concise evidence-based reason is required')
    if mode == 'edit' and execution != 'failed' and intent != 'misaligned':
        raise ValueError('Edit requires observed execution failure or misaligned next intent, not uncertainty alone')
    return assessment


def status_outputs(result):
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get('ok') is not True:
        return {}
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('response'), dict):
        return {}
    record = data['response'].get('result')
    if not isinstance(record, dict):
        return {}
    if record.get('status') != 'succeeded':
        return {}
    return record.get('outputs') or {}


class Supervisor:
    def __init__(self, frames_root):
        self.frames_root = Path(frames_root).resolve()
        self.status = {}
        self.frames = {}
        self.previous = None
        self.delivered_binding = None
        self.pending_decision = None

    def identity(self):
        episode = self.status.get('episode') or {}
        checkpoint = self.status.get('checkpoint') or {}
        return episode.get('index'), episode.get('steps'), checkpoint.get('checkpoint_id')

    def observe_status(self, status):
        identity = self.identity()
        self.status = copy.deepcopy(status)
        if identity != self.identity():
            self.frames = {}
            self.delivered_binding = None

    def observe_frame(self, tool_id, output):
        if not (self.status.get('checkpoint') or {}).get('checkpoint_id'):
            raise ValueError('Fetch images only after querying the current open checkpoint')
        if (self.status.get('candidate') or {}).get('freshness') != 'pending':
            raise ValueError('Fetch checkpoint images only after a pending candidate is available')
        camera = {'vision.get_frame': 'head', 'vision.get_frame_left_wrist': 'left_wrist',
                  'vision.get_frame_right_wrist': 'right_wrist'}.get(tool_id)
        if camera is None or not output.get('image_path'):
            return
        path = Path(output['image_path']).resolve()
        if not path.is_relative_to(self.frames_root) or not path.is_file():
            raise ValueError('Camera image is not owned by this trial')
        content = path.read_bytes()
        suffix = path.suffix.lower()
        mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}.get(suffix)
        if not mime or len(content) > 10_000_000:
            raise ValueError('Unsupported or oversized checkpoint image')
        self.frames[camera] = {'camera': camera, 'path': str(path),
            'sha256': hashlib.sha256(content).hexdigest(), 'frame_seq': output.get('frame_seq'),
            'data_url': 'data:' + mime + ';base64,' + base64.b64encode(content).decode()}

    def packet(self):
        checkpoint = self.status.get('checkpoint') or {}
        return {'supervision_version': VERSION, 'episode': self.status.get('episode'),
            'checkpoint': {key: checkpoint.get(key) for key in ('checkpoint_id', 'trigger', 'reason',
                'step_in_episode', 'last_segment', 'details')},
            'qpos': self.status.get('last_joint_positions'), 'candidate': self.status.get('candidate'),
            'full_future_chunk_visible': False,
            'images': [{key: frame[key] for key in ('camera', 'path', 'sha256', 'frame_seq')}
                       for frame in self.frames.values()],
            'previous_reviewed_boundary': None if self.previous is None else {
                key: value for key, value in self.previous['packet'].items()
                if key not in {'previous_reviewed_boundary', 'previous_accepted_assessment'}},
            'previous_accepted_assessment': None if self.previous is None else self.previous['assessment']}

    def binding(self):
        value = {'identity': self.identity(), 'candidate': self.status.get('candidate'),
                 'frames': {camera: frame['sha256'] for camera, frame in self.frames.items()}}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def augment_messages(self, messages):
        if not self.frames or not (self.status.get('checkpoint') or {}).get('checkpoint_id'):
            return messages, None
        packet = self.packet()
        content = [{'type': 'text', 'text': 'Current checkpoint evidence. Review actual previous execution '
            'and next intent separately. These are original camera images, not a textual vision summary. '
            'Candidate is only one next action; no future chunk/FK is available.\n' +
            json.dumps(packet, ensure_ascii=False)}]
        groups = [('current', self.frames)]
        if self.previous is not None:
            groups.insert(0, ('previous_reviewed_boundary', self.previous['frames']))
        for label, frames in groups:
            for camera, frame in frames.items():
                content.extend([{'type': 'text', 'text': label + ' camera=' + camera + ' sha256=' + frame['sha256']},
                    {'type': 'image_url', 'image_url': {'url': frame['data_url']}}])
        self.delivered_binding = self.binding()
        return [*messages, {'role': 'user', 'content': content}], {'binding': self.delivered_binding,
            'checkpoint_id': packet['checkpoint']['checkpoint_id'], 'step': (self.status.get('episode') or {}).get('steps'),
            'current_cameras': list(self.frames), 'previous_cameras': [] if self.previous is None else list(self.previous['frames'])}

    def check(self, name, params):
        tool_id = params.get('tool_id')
        if name == 'image':
            raise ValueError('Raw camera images are attached to the main decision request; no separate vision-summary tool')
        if tool_id == 'vla.set_mode' and params.get('arguments', {}).get('mode') != 'stop':
            raise ValueError('Unbounded student release is disabled; use an assessed vla.decide segment')
        if tool_id != 'vla.decide':
            return
        if name != 'forge_tool_call':
            raise ValueError('vla.decide must be a synchronous forge_tool_call')
        if 'head' not in self.frames or not {'left_wrist', 'right_wrist'}.intersection(self.frames):
            raise ValueError('Decision needs current head and at least one wrist image')
        if self.delivered_binding != self.binding():
            raise ValueError('Decision evidence changed or has not reached the main multimodal request; query and review again')
        assessment = validate_assessment(params.get('arguments') or {}, self.status)
        self.pending_decision = {'assessment': copy.deepcopy(assessment), 'packet': self.packet(),
                                 'frames': copy.deepcopy(self.frames), 'binding': self.binding()}

    def finish_decision(self, output):
        pending, self.pending_decision = self.pending_decision, None
        if output.get('accepted') is not True or pending is None:
            return None
        self.previous = pending
        self.frames = {}
        self.delivered_binding = None
        self.status = {}
        return {key: value for key, value in pending.items() if key != 'frames'}


def compact_status_result(result):
    try:
        payload = json.loads(result)
        outputs = payload['data']['response']['result']['outputs']
        outputs.pop('checkpoint_history', None)
        return json.dumps(payload, ensure_ascii=False)
    except (ValueError, KeyError, TypeError):
        return result

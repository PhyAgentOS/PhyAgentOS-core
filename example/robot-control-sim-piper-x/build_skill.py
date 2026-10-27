"""Reproducible Piper X model/profile extension of public robot-control-sim 0.1.2.

Uses the official AgileX Piper X URDF, not the bundled ordinary Piper model.
Runtime planning/control Nodes are preserved byte for byte.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import mujoco
import yaml

ROOT = Path(__file__).resolve().parent
UPSTREAM = None
AGX = None
DEST = None
HEADLESS = False
MODEL_REVISION = '680fa925cd00db7e2628563d2edc5540963045de'
PROFILE = 'mujoco-piper-x'
JOINTS = [f'joint{i}' for i in range(1, 7)]
INITIAL = dict(zip(JOINTS, [0.0, 0.65, -1.10, 0.25, 0.30, 0.10])) | {'gripper': 0.04}


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write_xml(root, path):
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


def yload(p):
    return yaml.safe_load(p.read_text())


def ywrite(p, d):
    p.write_text(yaml.safe_dump(d, sort_keys=False))


def main():
    revision = subprocess.check_output(['git', '-C', str(AGX), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != MODEL_REVISION:
        raise SystemExit(f'Model checkout must be {MODEL_REVISION}; found {revision}')
    if subprocess.check_output(['git', '-C', str(AGX), 'status', '--porcelain'], text=True).strip():
        raise SystemExit('Model checkout must be clean')
    manifest = yload(UPSTREAM / 'skill.yaml')
    if (manifest.get('name'), str(manifest.get('version'))) != ('robot-control-sim', '0.1.2'):
        raise SystemExit('Expected installed robot-control-sim 0.1.2')
    expected = json.loads((ROOT / 'node-locks.json').read_text())
    if manifest.get('artifacts', {}).get('nodes') != expected:
        raise SystemExit('Upstream Node locks differ from the demo baseline')
    if DEST.exists():
        raise SystemExit('Preserve existing source; remove only the generated tree explicitly before rebuilding.')
    shutil.copytree(UPSTREAM, DEST, ignore=shutil.ignore_patterns('archive-manifest.json'))
    robot = DEST / 'assets/robots/piper_x'
    model_dir = robot / 'mjcf'
    meshes = model_dir / 'meshes'
    meshes.mkdir(parents=True)
    source_path = AGX / 'piper_x/urdf/piper_x_description.urdf'
    gripper_path = AGX / 'piper_x/urdf/piper_x_with_gripper_description.xacro'
    root = ET.parse(source_path).getroot()
    for el in ET.parse(gripper_path).getroot():
        if not el.tag.startswith('{'):
            root.append(copy.deepcopy(el))
    root.append(ET.Element('link', {'name': 'tcp'}))
    tcp = ET.SubElement(root, 'joint', {'name': 'tcp_fixed', 'type': 'fixed'})
    ET.SubElement(tcp, 'parent', {'link': 'gripper_base'})
    ET.SubElement(tcp, 'child', {'link': 'tcp'})
    ET.SubElement(tcp, 'origin', {'xyz': '0 0 0.138', 'rpy': '0 0 0'})
    sources = [source_path, gripper_path]
    for link in root.findall('link'):
        for mesh in link.findall('.//mesh'):
            original = mesh.get('filename').split('agx_arm_urdf/', 1)[1]
            source = AGX / original
            if source.suffix == '.dae':
                source = source.parent.parent / (source.stem + '.stl')
            prefix = original.split('/')[0]
            filename = prefix + '_' + source.name
            shutil.copy2(source, meshes / filename)
            mesh.set('filename', 'mjcf/meshes/' + filename)
            sources.append(source)
        # Official STL shape is used for both visual/collision; DAE color is not retained.
        vis = link.find('visual')
        if vis is not None:
            material = ET.SubElement(vis, 'material', {'name': link.get('name') + '_gray'})
            ET.SubElement(material, 'color', {'rgba': '0.65 0.69 0.73 1'})
    write_xml(root, robot / 'piper_x_with_gripper.urdf')
    # The URDF's "gripper" is a massless logical opening used by ROS mimic.
    # MuJoCo instead has the two physical sliders coupled q2=-q1, and gear=2.
    sim_root = copy.deepcopy(root)
    for el in list(sim_root):
        if (el.tag == 'link' and el.get('name') == 'gripper_link') or (el.tag == 'joint' and el.get('name') == 'gripper'):
            sim_root.remove(el)
    for mesh in sim_root.findall('.//mesh'):
        mesh.set('filename', str(meshes / Path(mesh.get('filename')).name))
    extension = ET.SubElement(sim_root, 'mujoco')
    ET.SubElement(extension, 'compiler', {'discardvisual': 'false', 'fusestatic': 'false', 'strippath': 'false'})
    temp_urdf = model_dir / 'compile-input.urdf'
    write_xml(sim_root, temp_urdf)
    imported = mujoco.MjModel.from_xml_path(str(temp_urdf))
    intermediate = model_dir / 'imported.xml'
    mujoco.mj_saveLastXML(str(intermediate), imported)
    mj = ET.parse(intermediate).getroot()
    mj.set('model', 'agilex_piper_x')
    compiler = mj.find('compiler')
    compiler.set('meshdir', 'meshes')
    compiler.set('angle', 'radian')
    for mesh in mj.findall('./asset/mesh'):
        mesh.set('file', Path(mesh.get('file')).name)
    opt = ET.SubElement(mj, 'option', {'timestep': '0.002', 'integrator': 'implicitfast', 'gravity': '0 0 -9.81'})
    for body in mj.findall('.//body'):
        body.set('gravcomp', '1')
    for joint in mj.findall('.//body/joint'):
        joint.set('damping', '1')
        joint.set('armature', '0.01' if joint.get('name') in JOINTS[:3] else '0.002')
    tcp_body = next(b for b in mj.findall('.//body') if b.get('name') == 'tcp')
    ET.SubElement(tcp_body, 'site', {'name': 'tcp', 'size': '0.008', 'rgba': '1 0.2 0.1 1'})
    eq = ET.SubElement(mj, 'equality')
    ET.SubElement(eq, 'joint', {'joint1': 'gripper_joint2', 'joint2': 'gripper_joint1', 'polycoef': '0 -1 0 0 0'})
    actuators = ET.SubElement(mj, 'actuator')
    for i, joint in enumerate(root.findall('joint')):
        name = joint.get('name')
        if name not in JOINTS:
            continue
        lim = joint.find('limit')
        ET.SubElement(actuators, 'position', {'name': name, 'joint': name,
                      'kp': '600' if name in JOINTS[:3] else '120',
                      'kv': '35' if name in JOINTS[:3] else '8',
                      'ctrlrange': lim.get('lower') + ' ' + lim.get('upper'),
                      'forcerange': '-100 100'})
    ET.SubElement(actuators, 'position', {'name': 'gripper', 'joint': 'gripper_joint1',
                  'gear': '2', 'kp': '200', 'kv': '5', 'ctrlrange': '0 0.1', 'forcerange': '-10 10'})
    contact = ET.SubElement(mj, 'contact')
    # The fixed pedestal and shoulder housing overlap by design in the URDF.
    # Their convex mesh contact otherwise pins joint1. Ordinary upstream Piper
    # uses this same adjacent-body exclusion; all other contacts remain enabled.
    ET.SubElement(contact, 'exclude', {'body1': 'base_link', 'body2': 'link1'})
    write_xml(mj, model_dir / 'piper_x.xml')
    temp_urdf.unlink()
    intermediate.unlink()
    scene = ET.Element('mujoco', {'model': 'piper_x_scene'})
    ET.SubElement(scene, 'include', {'file': 'piper_x.xml'})
    visual = ET.SubElement(scene, 'visual')
    ET.SubElement(visual, 'global', {'offwidth': '1280', 'offheight': '960'})
    ET.SubElement(visual, 'headlight', {'ambient': '0.5 0.5 0.5', 'diffuse': '0.6 0.6 0.6'})
    asset = ET.SubElement(scene, 'asset')
    ET.SubElement(asset, 'texture', {'name': 'floor', 'type': '2d', 'builtin': 'checker', 'rgb1': '.18 .21 .24', 'rgb2': '.23 .27 .30', 'width': '512', 'height': '512'})
    ET.SubElement(asset, 'material', {'name': 'floor', 'texture': 'floor', 'texrepeat': '4 4', 'texuniform': 'true'})
    world = ET.SubElement(scene, 'worldbody')
    ET.SubElement(world, 'light', {'pos': '0 -1 2', 'dir': '0 0 -1'})
    ET.SubElement(world, 'geom', {'name': 'floor', 'type': 'plane', 'size': '2 2 .1', 'pos': '0 0 -0.002', 'material': 'floor'})
    ET.SubElement(world, 'camera', {'name': 'angle', 'pos': '1.0 -1.0 .85', 'xyaxes': '.707 .707 0 -.27 .27 .924', 'fovy': '48'})
    ET.SubElement(world, 'camera', {'name': 'top', 'pos': '.15 0 1.5', 'xyaxes': '1 0 0 0 1 0', 'fovy': '55'})
    write_xml(scene, model_dir / 'scene.xml')
    shutil.copy2(AGX / 'LICENSE', robot / 'LICENSE.agilex')
    limits = {j.get('name'): [float(j.find('limit').get('lower')), float(j.find('limit').get('upper'))]
              for j in root.findall('joint') if j.get('name') in JOINTS}
    descriptor = {'name': 'agilex_piper_x', 'source': 'mjcf', 'model': 'mjcf/scene.xml',
                  'joints': [{'name': j, 'kind': 'revolute', 'limits_rad': limits[j]} for j in JOINTS]
                           + [{'name': 'gripper', 'kind': 'prismatic', 'limits_m': [0, .1]}],
                  'eef_site': 'tcp', 'initial_qpos': INITIAL, 'has_gripper': True,
                  'notes': 'Official AgileX Piper X geometry/kinematics/inertias; simulation position servos with gravity compensation. TCP is defined at gripper_base + [0,0,0.138] m. No hardware calibration or collision-free planning claim.'}
    ywrite(robot / 'robot.yaml', descriptor)
    profile = DEST / 'profiles' / PROFILE
    shutil.copytree(DEST / 'profiles/mujoco-piper', profile)
    sim = yload(profile / 'simulator.yaml')
    sim['model_path'] = '../../assets/robots/piper_x/mjcf/scene.xml'
    sim['initial_state']['joint_positions'] = INITIAL
    sim['joints'][-1]['state_from_joints'] = ['gripper_joint1', 'gripper_joint2']
    sim['robots'][0]['prefix'] = ''
    sim['image_forward'] = {c: {'output': 'image/' + c, 'fps': 15, 'width': 640, 'height': 480} for c in ['angle', 'top']}
    ywrite(profile / 'simulator.yaml', sim)
    motion = yload(profile / 'motion_server.yaml')
    group = motion['groups'].pop('piper_arm')
    group.update(urdf_path='../../assets/robots/piper_x/piper_x_with_gripper.urdf', base_frame='base_link', tip_frame='tcp')
    motion['groups']['piper_x_arm'] = group
    ywrite(profile / 'motion_server.yaml', motion)
    policy = yload(profile / 'sim_control_policy.yaml')
    policy['robot_descriptor_path'] = '../../assets/robots/piper_x/robot.yaml'
    policy['reset']['motion']['arguments']['group_name'] = 'piper_x_arm'
    ywrite(profile / 'sim_control_policy.yaml', policy)
    grip = yload(profile / 'gripper_controller.yaml'); grip['max_position'] = .1
    ywrite(profile / 'gripper_controller.yaml', grip)
    gateway = yload(profile / 'gateway.yaml'); gateway['port'] = 19104
    for spec in gateway['tools']['specs']:
        frames = spec['robot_frame_profile']
        frames.update(robot_id='agilex_piper_x', base_frame='base_link', tool_frame='tcp')
        frames['directions']['forward']['frame'] = 'base_link'
        props = spec['input_schema']['properties']
        if 'group_name' in props: props['group_name'] = {'const': 'piper_x_arm'}
        if 'reference_frame' in props: props['reference_frame'] = {'const': 'base_link'}
        if 'target_frame' in props: props['target_frame'] = {'const': 'tcp'}
        for k in ['velocity_scale', 'acceleration_scale']:
            if k in props: props[k]['maximum'] = .3
        if spec['tool_id'] == 'sim.set_joint_positions':
            props['joint_names'] = {'const': JOINTS}
            props['positions'].update(minItems=6, maxItems=6)
        if 'opening_m' in props:
            props['opening_m']['maximum'] = .1
            spec['description'] = 'Set the Piper X parallel gripper logical opening, 0..0.1 meters.'
    ywrite(profile / 'gateway.yaml', gateway)
    flow = yload(profile / 'dataflow.yaml')
    sim_node = next(n for n in flow['nodes'] if n['id'] == 'mujoco')
    sim_node['outputs'] = ['proprio_state', 'image/angle', 'image/top']
    viewer = next(n for n in flow['nodes'] if n['id'] == 'image_viewer')
    viewer['inputs'] = {'image/' + c: 'mujoco/image/' + c for c in ['angle', 'top']}
    ywrite(profile / 'dataflow.yaml', flow)
    ywrite(profile / 'image_viewer.yaml', {'images': ['image/angle'], 'renderer': 'glow'})
    manifest = yload(DEST / 'skill.yaml')
    manifest.update(name='robot-control-sim-piper-x', version='0.1.2', gateway_url='http://127.0.0.1:19104',
                    description='AgileX Piper X MuJoCo control through native joint and end-effector pose Actions, with the original robot-control-sim planning/control Nodes.')
    new = copy.deepcopy(manifest['profiles']['mujoco-piper'])
    new['dataflow'] = f'profiles/{PROFILE}/dataflow.yaml'
    new['required_assets'] = ['assets/robots/piper_x/' + p for p in ['robot.yaml', 'mjcf/scene.xml', 'mjcf/piper_x.xml', 'piper_x_with_gripper.urdf']]
    new['environment'] = ({'DISPLAY': ':92', 'LIBGL_ALWAYS_SOFTWARE': '1', 'XDG_RUNTIME_DIR': '/tmp/paos-piper-x-xvfb'} if HEADLESS else {})
    # This separately named deployment includes only Piper X. The pristine
    # original bundle remains alongside it; its robots/ports are not overwritten.
    manifest['profiles'] = {PROFILE: new}
    # Resolve the original locked archives through the configured registry.
    manifest['artifacts']['resolver'] = 'registry'
    ywrite(DEST / 'skill.yaml', manifest)
    if HEADLESS:
        for name in ['start.sh', 'stop_display.sh']:
            shutil.copy2(ROOT / 'headless' / name, DEST / name)
            (DEST / name).chmod(0o755)
    for directory in (DEST / 'profiles').iterdir():
        if directory.name != PROFILE: shutil.rmtree(directory)
    for directory in (DEST / 'assets/robots').iterdir():
        if directory.name != 'piper_x': shutil.rmtree(directory)
    generator = DEST / 'scripts/gen_robot_table.py'
    generator.write_text(generator.read_text().replace('ROBOT_PROFILES = {', 'ROBOT_PROFILES = {\n    "piper_x": "mujoco-piper-x",'))
    skill = DEST / 'SKILL.md'
    skill.write_text(SKILL_DOCUMENT)
    subprocess.run([__import__('sys').executable, str(generator)], check=True)
    revision = subprocess.check_output(['git', '-C', str(AGX), 'rev-parse', 'HEAD'], text=True).strip()
    provenance = {'model_source': 'https://github.com/agilexrobotics/agx_arm_urdf', 'revision': revision,
                  'source_files': {str(p.relative_to(AGX)): sha(p) for p in sorted(set(sources))},
                  'mujoco_builder_version': mujoco.__version__, 'upstream_skill': 'robot-control-sim 0.1.2',
                  'upstream_manifest_sha256': sha(UPSTREAM / 'skill.yaml'),
                  'changes': ['Expand official gripper xacro include without ROS', 'Use provided STL meshes for visuals as well as collision',
                              'Add explicitly defined tcp fixed frame at gripper_base z=0.138m',
                              'Remove massless logical gripper from MuJoCo, couple physical sliders with equality and gear=2 actuator',
                              'Add tunable simulation position servos, damping/armature and gravity compensation; not calibrated hardware dynamics',
                              'Exclude only adjacent base_link/link1 contact to remove designed-in shoulder housing overlap, as in upstream ordinary Piper',
                              'Add Piper X-only deployment profile; pristine original bundle retained separately; runtime Nodes unchanged']}
    (robot / 'PROVENANCE.json').write_text(json.dumps(provenance, indent=2))
    print(json.dumps({'destination': str(DEST), 'model_revision': revision, 'joint_limits': limits, 'initial': INITIAL}, indent=2))


SKILL_DOCUMENT = '''---
name: robot-control-sim-piper-x
description: Control an AgileX Piper X in MuJoCo through joint targets or absolute end-effector poses.
metadata: {"PhyAgentOS":{"always":false,"requires":{"runtime":["robot-control-sim-piper-x"]}}}
---

# Piper X MuJoCo control / Piper X 仿真控制

This is a Piper X-only derivative of public robot-control-sim 0.1.2. It uses the
official AgileX Piper X model, not ordinary Piper. All nine runtime Nodes retain
the original immutable hashes. Simulation only; no hardware interfaces exist.

## Workflow / 工作流

1. Select profile `mujoco-piper-x`. Activate this Skill as primary, discover
   `sim.get_robot_info`, then obtain live context/schema of the intended tool.
   Require readiness and identity agilex_piper_x. The descriptor is model metadata,
   not a live joint-state Query.
2. Create one AgentTask for the explicit user goal and measurable success criteria.
   Bind the requested Action to this task. For encoder/FK-only verification choose
   audit mode, minimum_association=best_effort, required_kinds=[], required_sources=[].
   In verification.constraints include: Verifier evidence_refs must copy exact
   strings from valid_evidence_refs; do not invent concise labels or prefixes such
   as gateway_terminal_result: or tool_execution_record:.
3. Path 1: `sim.set_joint_positions` sends six absolute radians, exactly ordered
   [joint1,joint2,joint3,joint4,joint5,joint6], group_name=piper_x_arm.
   Path 2: `sim.move_eef` sends target_pose={x,y,z,qx,qy,qz,qw}, position in meters,
   unit quaternion in xyzw fields, reference_frame=base_link, target_frame=tcp,
   group_name=piper_x_arm. The existing motion_server solves IK and plans the
   trajectory; never calculate joints and substitute joint control for a pose goal.
4. Use explicit speed and acceleration scales in (0,0.3], typically 0.2. Reject
   ambiguous/out-of-limits goals; never silently clip or invent a pose. TCP is
   gripper_base + [0,0,0.138] m. It is a simulation frame definition, not calibrated
   hardware TCP. Usual test tolerances: 0.005 m position and 0.03 rad orientation.
5. Submit exactly one requested motion. Admission is not completion. Retain its ID,
   poll status/result at most 12 times, and require a terminal succeeded result plus
   motion_result.error_code=SUCCESS. The raw Gateway invocation phase is completed;
   success is in result.status=succeeded, not phase=succeeded. For joints require max absolute residual
   <=0.03 rad; for EEF require both returned pose residuals within requested limits.
   Never automatically resend an unknown/failed Action. Do not use exec/shell,
   direct HTTP, robot SDK or simulator state mutation. Use framework tools only.
6. Finalize after all owned execution is terminal, then inspect forge_task_get.
   Report execution facts separately from the persisted independent verdict.
   audit status=succeeded with verdict=null is not verification success.

## Supported robot / 机器人

<!-- BEGIN ROBOT TABLE (managed by scripts/gen_robot_table.py; do not edit) -->
<!-- END ROBOT TABLE -->

Parallel gripper: sim.set_gripper_opening accepts 0..0.1 meters. sim.reset resets
physics and re-homes through the original native motion/gripper tool pipeline.
Reset or gripper commands are separate user requests, never automatic motion retries.

## Runtime / 运行

Gateway 127.0.0.1:19104. The optional headless profile uses Xvfb :92 and glow/software OpenGL.
Start through paos skill start; for headless builds stop the runtime then run stop_display.sh.
Gravity-compensated position servos are simulation tuning, not identified hardware
dynamics. Pose planning does not establish obstacle avoidance or grasping success.
'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', type=Path, required=True, help='Installed robot-control-sim 0.1.2 directory')
    parser.add_argument('--models', type=Path, required=True, help='Clean official AgileX model checkout at pinned revision')
    parser.add_argument('--output', type=Path, required=True, help='New output directory; existing paths are refused')
    parser.add_argument('--headless', action='store_true', help='Linux Xvfb :92 + software OpenGL; requires Xvfb')
    args = parser.parse_args()
    UPSTREAM, AGX, DEST = args.upstream.resolve(), args.models.resolve(), args.output.resolve()
    HEADLESS = args.headless
    main()

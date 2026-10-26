"""Independent URDF-vs-MuJoCo FK plus servo stability checks (no PAOS bypass acceptance)."""
import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import mujoco
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent
ROBOT = ROOT / 'robot-control-sim-piper-x/assets/robots/piper_x'
JOINTS = [f'joint{i}' for i in range(1, 7)]


def rotation(rpy):
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array([[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr],
                     [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr], [-sp, cp*sr, cp*cr]])


def fk(q):
    urdf = ET.parse(ROBOT / 'piper_x_with_gripper.urdf').getroot()
    by_child = {j.find('child').get('link'): j for j in urdf.findall('joint')}
    chain, child = [], 'tcp'
    while child != 'base_link':
        j = by_child[child]; chain.append(j); child = j.find('parent').get('link')
    t = np.eye(4)
    for j in reversed(chain):
        origin = j.find('origin')
        transform = np.eye(4)
        if origin is not None:
            transform[:3, 3] = np.fromstring(origin.get('xyz', '0 0 0'), sep=' ')
            transform[:3, :3] = rotation(np.fromstring(origin.get('rpy', '0 0 0'), sep=' '))
        t = t @ transform
        if j.get('name') in JOINTS:
            axis = np.fromstring(j.find('axis').get('xyz'), sep=' ')
            theta = q[JOINTS.index(j.get('name'))]
            k = np.array([[0,-axis[2],axis[1]],[axis[2],0,-axis[0]],[-axis[1],axis[0],0]])
            transform = np.eye(4)
            transform[:3, :3] = np.eye(3) + math.sin(theta)*k + (1-math.cos(theta))*(k@k)
            t = t @ transform
    quat = np.zeros(4); mujoco.mju_mat2Quat(quat, t[:3,:3].reshape(-1))
    pose = dict(zip(['x','y','z','qw','qx','qy','qz'], [*t[:3,3], *quat]))
    return t, pose


def main():
    descriptor = yaml.safe_load((ROBOT/'robot.yaml').read_text())
    model = mujoco.MjModel.from_xml_path(str(ROBOT/'mjcf/scene.xml'))
    data = mujoco.MjData(model)
    ids = [model.joint(j).id for j in JOINTS]
    qidx = [model.jnt_qposadr[i] for i in ids]
    initial = [descriptor['initial_qpos'][j] for j in JOINTS]
    rng = np.random.default_rng(20261020)
    cases = [initial] + [rng.uniform([-.5,.2,-1.7,-.5,-.6,-.8], [.5,1.3,-.4,.5,.6,.8]).tolist() for _ in range(30)]
    rows = []
    for q in cases:
        data.qpos[qidx] = q; mujoco.mj_forward(model,data)
        t, pose = fk(q)
        pos_err = float(np.linalg.norm(data.site('tcp').xpos-t[:3,3]))
        rot_err = float(np.max(np.abs(data.site('tcp').xmat.reshape(3,3)-t[:3,:3])))
        assert pos_err < 1e-6 and rot_err < 1e-6, (pos_err,rot_err)
        rows.append({'q':q,'position_error_m':pos_err,'rotation_matrix_max_error':rot_err})
    # Test the same model's physical servos separately from runtime acceptance.
    mujoco.mj_resetData(model,data)
    data.qpos[qidx] = initial
    data.joint('gripper_joint1').qpos[:] = .02
    data.joint('gripper_joint2').qpos[:] = -.02
    data.ctrl[:] = initial + [.04]
    mujoco.mj_forward(model,data)
    initial_contacts = data.ncon
    for _ in range(2500): mujoco.mj_step(model,data)
    residual = np.abs(data.qpos[qidx]-initial)
    assert np.isfinite(data.qpos).all() and max(residual)<.03, residual
    targetq = [.12,.75,-1.18,.33,.38,.18]
    _, target = fk(targetq)
    report = {'mujoco_version':mujoco.__version__,'fk_cases':len(rows),
              'max_fk_position_error_m':max(r['position_error_m'] for r in rows),
              'max_fk_rotation_matrix_error':max(r['rotation_matrix_max_error'] for r in rows),
              'cases':rows,'initial_contacts':initial_contacts,'final_contacts':data.ncon,
              'hold_max_error_rad':float(max(residual)), 'initial_pose':fk(initial)[1],
              'eef_target_seed_joints':targetq,'eef_target_pose':target,
              'actuator_order':[model.actuator(i).name for i in range(model.nu)],
              'physical_dof':model.nq,'logical_dof':len(descriptor['joints'])}
    (ROOT/'model-validation.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    ROBOT = args.skill.resolve() / 'assets/robots/piper_x'
    ROOT = args.output_dir.resolve()
    ROOT.mkdir(parents=True, exist_ok=True)
    main()

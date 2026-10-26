"""Live acceptance through native ForgeToolClient and the original Dora pipeline.

Never writes simulator state, solves IK, or replays an unknown Action.
Pose targets are precomputed reachable fixtures; only Cartesian poses go to Tool API.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from PhyAgentOS.forge.tool_client import ForgeToolClient, ForgeToolAPIError

ROOT=Path(__file__).resolve().parent
JOINTS=[f'joint{i}' for i in range(1,7)]


def find_motion(value):
    if isinstance(value,dict):
        if 'error_code' in value and 'joint_names' in value: return value
        for child in value.values():
            found=find_motion(child)
            if found is not None: return found
    return None


async def execute(c, label, tool, arguments, destination, expected_success=True):
    row={'label':label,'tool_id':tool,'arguments':arguments,
         'started_at':datetime.now(timezone.utc).isoformat(),'status_samples':[]}
    def save(): destination.write_text(json.dumps(row,indent=2))
    save()
    try:
        row['context']=await c.get_tool_context(tool)
        assert row['context']['data']['ready'], row['context']
        row['admission']=await c.invoke_action(tool,arguments,timeout_ms=20000)
        ident=row['admission']['data']['invocation_id']; save()
        deadline=time.monotonic()+70
        while time.monotonic()<deadline:
            status=await c.invocation_status(ident)
            row['status_samples'].append(status)
            phase=status.get('data',{}).get('phase')
            # A result exchange during dispatch can race provider admission in
            # upstream Gateway 1.0.2. Poll status first; never reinterpret an
            # actual unknown result as pending or automatically resend motion.
            if phase=='completed' or phase in {'failed','cancelled','canceled','timed_out','unknown'}:
                result=await c.invocation_result(ident)
                row['result']=result;save()
                break
            save()
            await asyncio.sleep(.5)
        else:
            row['cancel_request']=await c.cancel_invocation(ident); save()
            raise RuntimeError('Action did not reach terminal; cancellation requested, no resend')
        row['motion_result']=find_motion(row['result'])
        terminal=row['result'].get('data',{}).get('result',{})
        assert terminal.get('status')==('succeeded' if expected_success else 'failed'), row['result']
        motion=row['motion_result']
        if motion is not None:
            succeeded=motion['error_code']=='SUCCESS'
            assert succeeded==expected_success, motion
            if succeeded and tool=='sim.set_joint_positions':
                err=max(abs(float(x)) for x in motion['final_position_errors'])
                assert motion['joint_names']==JOINTS and err<=.03, motion
                row['max_joint_error_rad']=err
            if succeeded and tool=='sim.move_eef':
                assert motion['final_position_error_m']<=arguments['position_tolerance_m'], motion
                assert motion['final_orientation_error_rad']<=arguments['orientation_tolerance_rad'], motion
        else:
            assert tool in {'sim.reset','sim.set_gripper_opening'}, row['result']
            assert row['status_samples'][-1]['data']['phase']=='completed', row['result']
            outputs=terminal.get('outputs',{})
            if tool=='sim.set_gripper_opening':
                assert outputs['gripper_result']['error_code']=='SUCCESS' and outputs['gripper_result']['reached_goal'],outputs
            if tool=='sim.reset':
                assert outputs['episode_reset'] and outputs['rehomed'],outputs
        row['check_passed']=True
    except Exception as exc:
        row.update(check_passed=False,error_type=type(exc).__name__,error=str(exc),error_payload=getattr(exc,'payload',None))
        raise
    finally:
        row['finished_at']=datetime.now(timezone.utc).isoformat();save()
        print(json.dumps({k:row[k] for k in ['label','check_passed','motion_result','max_joint_error_rad','error'] if k in row}),flush=True)
    return row


async def main(args):
    output=args.output_dir.resolve()/args.run_id; output.mkdir(parents=True,exist_ok=False)
    fixtures=json.loads((ROOT/'motion-fixtures.json').read_text())
    summary={'run_id':args.run_id,'scope':'native Tool API -> unchanged motion Nodes -> MuJoCo','actions':[]}
    try:
        async with ForgeToolClient(args.gateway_url) as c:
            summary['tools']=await c.list_tools()
            summary['robot_info']=await c.invoke_query_tool('sim.get_robot_info',{})
            (output/'discovery.json').write_text(json.dumps(summary,indent=2))
            # Query result is checked by output schema; robot identity must be explicit.
            assert 'agilex_piper_x' in json.dumps(summary['robot_info'])
            if args.only:
                fixtures=[f for f in fixtures if f['label']==args.only]
                assert len(fixtures)==1, args.only
            for f in fixtures:
                row=await execute(c,f['label'],f['tool_id'],f['arguments'],output/(f['label']+'.json'),f.get('expected_success',True))
                summary['actions'].append({'label':f['label'],'passed':row['check_passed'],'invocation_id':row['admission']['data']['invocation_id']})
            summary['passed']=all(x['passed'] for x in summary['actions'])
    finally:
        (output/'summary.json').write_text(json.dumps(summary,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id',required=True)
    p.add_argument('--only')
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--gateway-url',default='http://127.0.0.1:19104')
    asyncio.run(main(p.parse_args()))

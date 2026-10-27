# Piper X MuJoCo Skill development example

[English](README.md) | [简体中文](README_zh.md)

For the full procedure, see the [development tutorial](../../docs/user_development_guide/PIPER_X_DEMO.md).

This example derives a Skill from installed public `robot-control-sim` 0.1.2,
using the pinned official AgileX Piper X model. It keeps all nine original Node
locks. It does not download or execute robot hardware drivers.

- `build_skill.py`: portable model/profile generator; refuses existing output.
- `node-locks.json`: expected original Node lock declarations.
- `validate_model.py`: URDF/MJCF FK and simulation servo checks.
- `accept_pipeline.py`: diagnostic native Tool API acceptance, without an LLM.
- `motion-fixtures.json`: synthetic reachable joint and Cartesian targets.
- `headless/`: optional Linux Xvfb hooks; display :92 must be available.

Generated artifacts, local logs and downloaded model assets belong under `work/`
and are ignored. The AgileX model license is copied into the generated Skill;
upstream Skill files retain their notices. Protocol/source lookup:
[TrackB framework guide](../../docs/user_development_guide/track_b/README.md).

The generator targets Piper X specifically. For another robot, use the model,
joint/frame mapping and acceptance checklist in the tutorial; it is not a generic
URDF import interface. The tutorial also documents one-target diagnostics with
`--only joint-a` and `--only eef-a`.

A locally installed derived Skill is not automatically registered or published
on a resource platform. Reuse matching published Node identities, submit the new
Skill through the target platform's registration/review workflow, and verify its
consumer API after publication. Source Node changes require new Node versions
and updated Skill locks.

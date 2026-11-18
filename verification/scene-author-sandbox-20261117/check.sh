#!/bin/bash
source /home/isaac/isaaclab30_env.sh
cd /opt/data/private/isaaclab/request-authoring/c
exec /home/isaac/IsaacLab30/.venv/bin/python tools/scene_assemble.py check \
  --spec /opt/data/private/isaaclab/request-authoring/runs/1b-proot-20261117-233440/scene-layout.json \
  --work /opt/data/private/isaaclab/request-authoring/runs/1b-proot-20261117-233440/prepared/78c0d8dceaa3310a \
  --headless

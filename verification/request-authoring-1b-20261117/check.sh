#!/bin/bash
source /home/isaac/isaaclab30_env.sh >/dev/null 2>&1
cd /opt/data/private/isaaclab/request-authoring/b
which python
timeout 1500 python tools/scene_assemble.py check --spec /opt/data/private/isaaclab/request-authoring/runs/1b-20261117-023625/scene-layout.json --work /opt/data/private/isaaclab/request-authoring/runs/1b-20261117-023625/prepared/854fc489b3884bc0 --headless
echo check_exit=$?

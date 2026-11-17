#!/bin/bash
source /home/isaac/isaaclab30_env.sh
export PATH=/home/isaac/node/bin:$PATH
# local Anthropic->OpenAI adapter on this host; placeholder token, not a credential
export ANTHROPIC_BASE_URL=http://127.0.0.1:4001 ANTHROPIC_AUTH_TOKEN=dummy
cd /opt/data/private/isaaclab/request-authoring/c
exec .venv/bin/python tools/scene_author.py --request examples/request/plate-drawer-airbot.yaml \
  --work /opt/data/private/isaaclab/request-authoring/runs/1b-proot-20261117-233440 \
  --cache /opt/data/private/isaaclab/simready-cache \
  --robot-package /opt/data/private/isaaclab/a-scene/runtime/robot_assets_isaaclab3_validated_20261113 \
  --agent-config /opt/data/private/isaaclab/request-authoring/runs/1b-proot-20261117-233440/scene-author.json --max-rounds 3 \
  --prepare-python /home/isaac/IsaacLab30/.venv/bin/python --endpoint https://hf-mirror.com

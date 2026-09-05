#!/usr/bin/env bash
# 远程节点安装：打包本地仓库 → 上传 → venv → systemd 常驻
# 用法: bash scripts/install_node.sh <host> <node_id> <node_token> [ssh-key-path]
set -euo pipefail
HOST=$1; NODE_ID=$2; TOKEN=$3; KEY=${4:-$HOME/.ssh/boris_wu_node}
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new root@$HOST"
SCP="scp -i $KEY"
ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
REMOTE=/opt/agent-fabric

echo "==> 1/5 打包"
tar -C "$ROOT_DIR" --exclude .venv --exclude data --exclude .git --exclude '*.db' -czf /tmp/agent-fabric.tgz .
echo "==> 2/5 上传到 $HOST"
$SCP /tmp/agent-fabric.tgz "$HOST:/tmp/"
$SSH "mkdir -p $REMOTE && tar -xzf /tmp/agent-fabric.tgz -C $REMOTE && rm /tmp/agent-fabric.tgz"
echo "==> 3/5 venv + 安装"
$SSH "cd $REMOTE && (python3 -m venv .venv 2>/dev/null || (apt-get update -qq && apt-get install -y -qq python3-venv && python3 -m venv .venv)) && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q ."
echo "==> 4/5 配置 env"
CENTRAL_URL=${AF_CENTRAL_URL:-"ws://$(curl -4s https://ip.sb):8000"}
$SSH "cat > $REMOTE/.env <<EOF
AF_NODE_ID=$NODE_ID
AF_NODE_TOKEN=$TOKEN
AF_CENTRAL_URL=$CENTRAL_URL
AF_WORKSPACE=$REMOTE/workspace
$(env | grep -E '^AF_(ALLOW_SHELL|OPENCODE_BIN|OPENCODE_MODEL)=' || true)
EOF
mkdir -p $REMOTE/workspace"
echo "==> 5/5 systemd"
$SSH "cp $REMOTE/deploy/systemd/fabric-node.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now fabric-node && sleep 2 && systemctl is-active fabric-node"
echo "完成：$NODE_ID @ $HOST → $CENTRAL_URL"

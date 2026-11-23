#!/usr/bin/env bash
# One-time setup on a fresh GPU instance (Ubuntu 22.04/24.04 with NVIDIA drivers, e.g. the AWS
# "Deep Learning Base OSS Nvidia Driver GPU AMI"). Run from the project folder:
#     bash deploy/setup_gpu.sh            # TENSORRT=1 bash deploy/setup_gpu.sh to also build a TensorRT detector
set -euo pipefail
cd "$(dirname "$0")/.."
unset LD_LIBRARY_PATH  # the AMI's older cuDNN would shadow PyTorch's own (see start.sh)

nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg python3-venv curl

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt -r requirements-gpu.txt
.venv/bin/python -c "import torch; assert torch.cuda.is_available(), 'PyTorch cannot see the GPU'; torch.nn.Conv2d(3, 8, 3).cuda()(torch.zeros(1, 3, 64, 64, device='cuda')); print('PyTorch GPU:', torch.cuda.get_device_name(0))"

[ -f models/yolo11s.pt ] || curl -sL --create-dirs -o models/yolo11s.pt \
    https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s.pt

if [ ! -f .env ]; then
    cat > .env <<'ENV'
# Moondream runs on this machine (python -m moondream_server.serve); the key isn't checked.
MOONDREAM_ENDPOINT=http://127.0.0.1:2020/v1
MOONDREAM_API_KEY=local
MAX_WORKERS=4
ENV
    echo "Wrote .env for the local Moondream server"
fi

if [ "${TENSORRT:-0}" = 1 ]; then
    # Person detector as a TensorRT FP16 engine: ~3x faster than PyTorch on the GPU. Built for this GPU only.
    .venv/bin/pip install -q tensorrt-cu12 onnx onnxslim
    .venv/bin/yolo export model=models/yolo11s.pt format=engine half=True imgsz=640 device=0
    grep -q '^PERSON_MODEL=' .env || echo 'PERSON_MODEL=models/yolo11s.engine' >> .env
fi

echo
echo "Setup done. Start with: bash deploy/start.sh"

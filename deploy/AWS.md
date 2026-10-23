# Our AWS GPU server

One GPU instance runs the web app and Moondream together (see "Live demo on a GPU" in the [README](../README.md)). This page lists what exists in AWS and how to start, stop, remove or recreate it.

## What exists

| | |
|---|---|
| Region | `ap-southeast-5` (Malaysia). The account's GPU quota is here; other regions may refuse GPU launches. |
| Instance | `i-09de1ae615a5cb48d`, name `ppe-detection-gpu`, `g6.2xlarge` (NVIDIA L4 24 GB, 8 vCPU, 32 GB RAM) |
| Image | Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04) |
| Disk | 100 GB gp3, deleted together with the instance |
| Network | public subnet `subnet-04a7aed25d6d3f50e` of VPC `vpc-0eb4b50519b36e783` (`robolyze-demo-vpc`) |
| Security group | `sg-0bd7a973302a9599d` (`ppe-detection-sg`): SSH (port 22) from one IP only. Port 8000 is not open; use an SSH tunnel. |
| SSH key | key pair `pavevqa-key-my`, private key `~/.ssh/pavevqa-key-my.pem` on the laptop (never in the repo), user `ubuntu` |
| On the instance | repository in `~/ppe_detection` (branch `feat/aws_compute`), set up with `deploy/setup_gpu.sh`. Moondream weights cached in `~/.cache/huggingface` (9.8 GB). |

- The public IP changes every time the instance starts.
- A running instance is billed by the hour. A stopped one costs only its disk.
- Malaysia offers `g6` (L4 24 GB) and `g7e` (RTX PRO 6000 96 GB) GPU instances, but not `g5`.

## 0. AWS keys on the laptop

The keys are temporary SSO keys and expire after a few hours (the error is `ExpiredToken` or `RequestExpired`). For fresh ones, open the AWS access portal, click **Access keys** next to the account, and copy the three `AWS_...` lines into `.env`. `.env` is git-ignored. Then, in the terminal you'll use:

```bash
eval "$(grep -E '^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)=' .env | sed 's/^/export /')"
export AWS_DEFAULT_REGION=ap-southeast-5
I=i-09de1ae615a5cb48d
SG=sg-0bd7a973302a9599d
KEY=~/.ssh/pavevqa-key-my.pem
aws sts get-caller-identity        # shows the account if the keys work
```

All the commands below use these variables.

## Start (each day)

1. **Start the instance** and get its IP:
   ```bash
   aws ec2 start-instances --instance-ids $I
   aws ec2 wait instance-running --instance-ids $I
   IP=$(aws ec2 describe-instances --instance-ids $I --query 'Reservations[0].Instances[0].PublicIpAddress' --output text); echo $IP
   ```
2. **If SSH times out**, your IP has probably changed. Allow the current one, and remove the old one:
   ```bash
   aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 22 --cidr "$(curl -s https://checkip.amazonaws.com)/32"
   aws ec2 describe-security-groups --group-ids $SG --query 'SecurityGroups[0].IpPermissions[].IpRanges[].CidrIp' --output text
   aws ec2 revoke-security-group-ingress --group-id $SG --protocol tcp --port 22 --cidr <old-ip>/32
   ```
3. **Start the app** inside `tmux`, so it keeps running when SSH disconnects:
   ```bash
   ssh -i $KEY ubuntu@$IP
   tmux new -s ppe                   # next time: tmux attach -t ppe
   cd ~/ppe_detection && git pull
   bash deploy/start.sh
   ```
   If `git pull` changed `requirements*.txt`, run `bash deploy/setup_gpu.sh` before `start.sh`. Running it again is safe. Moondream loads in about 2 minutes (the weights are already downloaded). Wait for `Uvicorn running on http://127.0.0.1:8000`.
4. **Open the app** from the laptop through a tunnel, in a second terminal:
   ```bash
   ssh -i ~/.ssh/pavevqa-key-my.pem -L 8000:localhost:8000 ubuntu@<IP>
   ```
   then go to http://localhost:8000.

## Stop (end of day)

```bash
aws ec2 stop-instances --instance-ids $I
aws ec2 wait instance-stopped --instance-ids $I
```

If the keys have expired, `ssh -i $KEY ubuntu@$IP 'sudo shutdown -h now'` also stops the instance, because its shutdown behaviour is "stop". The disk, the setup and the Moondream weights are kept either way.

## Remove everything

When the demo is over and the instance isn't needed again:

```bash
aws ec2 terminate-instances --instance-ids $I
aws ec2 wait instance-terminated --instance-ids $I
aws ec2 delete-security-group --group-id $SG
```

Terminating deletes the disk too, so the setup and the weights are gone.

## Recreate from scratch

Use this if the instance was terminated, or if you need a second one. On the laptop, with the keys loaded:

```bash
AMI=$(aws ssm get-parameter --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id --query Parameter.Value --output text)
ROOT=$(aws ec2 describe-images --image-ids $AMI --query 'Images[0].RootDeviceName' --output text)
# Skip these two lines if sg-0bd7a973302a9599d still exists:
SG=$(aws ec2 create-security-group --group-name ppe-detection-sg --description "PPE detection GPU box: SSH only" --vpc-id vpc-0eb4b50519b36e783 --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 22 --cidr "$(curl -s https://checkip.amazonaws.com)/32"
I=$(aws ec2 run-instances --image-id $AMI --instance-type g6.2xlarge --key-name pavevqa-key-my \
  --subnet-id subnet-04a7aed25d6d3f50e --associate-public-ip-address --security-group-ids $SG \
  --block-device-mappings "DeviceName=$ROOT,Ebs={VolumeSize=100,VolumeType=gp3,DeleteOnTermination=true}" \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=ppe-detection-gpu}]' 'ResourceType=volume,Tags=[{Key=Name,Value=ppe-detection-gpu}]' \
  --query 'Instances[0].InstanceId' --output text); echo $I
```

Write the new instance and security group IDs into the table above. Then do steps 1–2 of **Start** to get the IP, and on the instance:

```bash
ssh -i $KEY ubuntu@$IP
git clone -b feat/aws_compute https://github.com/brrikcy/ppe_detection && cd ppe_detection
bash deploy/setup_gpu.sh           # about 10 minutes; it prints almost nothing while apt and pip run
```

Then continue with step 3 of **Start**. The first `start.sh` downloads the Moondream weights (about 10 GB).

`g6.2xlarge` needs 8 vCPUs of the "Running On-Demand G and VT instances" quota. Our SSO role can't open Service Quotas, so if a launch fails with `VcpuLimitExceeded`, ask the AWS admin to check the quota.

## Problems we hit

- **`setup_gpu.sh` prints only the GPU name for minutes.** That's normal: apt and pip run quietly. The next line is `PyTorch GPU: NVIDIA L4`.
- **`CUDNN_STATUS_SUBLIBRARY_LOADING_FAILED`, or "Couldn't warm up the person detector".** The AMI puts its own older cuDNN on `LD_LIBRARY_PATH`, and PyTorch loads that instead of the cuDNN it ships with. `setup_gpu.sh` and `start.sh` clear the variable. If you run Python on the instance by hand (`moondream_server.bench`, `yolo export`), run `unset LD_LIBRARY_PATH` first.
- **`ExpiredToken` / `RequestExpired` from `aws`.** The keys have expired; see step 0.

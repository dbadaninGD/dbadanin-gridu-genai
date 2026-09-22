# GCP Deployment Instructions

Deploys the Synthetic Data Engine to a single Google Compute Engine VM using the `gcloud` CLI. Every resource is named with the `dbadanin` prefix, as requested.

> **What changed from the previous version of this guide:** the final step (`sudo docker-compose up -d --build`) was failing with `build path ... does not exist`. That was caused by `docker-compose.yml` living inside `backend/` with build contexts of `./backend` and `./frontend` — paths that are resolved *relative to the compose file*, so from inside `backend/` they pointed at the non-existent `backend/backend` and `backend/frontend`. `docker-compose.yml` now lives at the repo root, where `./backend` and `./frontend` correctly resolve to the two service directories. The rest of this guide reflects that, plus a couple of other fixes noted inline.

## Prerequisites

- The `gcloud` CLI installed and authenticated (`gcloud auth login`) with a GCP project that has billing enabled.
- Permission to create networks, firewall rules, service accounts, IAM bindings, and Compute Engine instances in that project.

```bash
export PROJECT_ID="your-gcp-project-id"
export ZONE="us-central1-a"
export REGION="us-central1"
export PREFIX="dbadanin"

gcloud config set project "$PROJECT_ID"
gcloud services enable compute.googleapis.com aiplatform.googleapis.com
```

## 1. Create a network and firewall rules

```bash
gcloud compute networks create ${PREFIX}-vpc --subnet-mode=auto

gcloud compute networks subnets update ${PREFIX}-vpc \
    --region=$REGION \
    --enable-private-ip-google-access

gcloud compute firewall-rules create ${PREFIX}-allow-ssh \
    --network=${PREFIX}-vpc \
    --allow=tcp:22 \
    --source-ranges=0.0.0.0/0 \
    --description="SSH access"

gcloud compute firewall-rules create ${PREFIX}-allow-web \
    --network=${PREFIX}-vpc \
    --allow=tcp:8000,tcp:8501 \
    --source-ranges=0.0.0.0/0 \
    --description="Streamlit frontend (8501) and FastAPI backend (8000)"
```

`0.0.0.0/0` is fine to get a demo running quickly, but it exposes the app (and SSH) to the entire internet. For anything longer-lived, scope both rules down to your own IP instead:

```bash
MY_IP=$(curl -s ifconfig.me)
gcloud compute firewall-rules update ${PREFIX}-allow-ssh --source-ranges="${MY_IP}/32"
gcloud compute firewall-rules update ${PREFIX}-allow-web --source-ranges="${MY_IP}/32"
```

## 2. Create a service account for Vertex AI

```bash
gcloud iam service-accounts create ${PREFIX}-sa \
    --description="Service account for the Synthetic Data Engine" \
    --display-name="${PREFIX}-sa"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${PREFIX}-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"
```

The VM will run as this service account, so the backend authenticates to Vertex AI automatically via the instance metadata server — **you do not need to run `gcloud auth application-default login` on the VM.** (That step is only for running the backend on your own machine; see `backend/README.md`.)

## 3. Provision the VM

```bash
gcloud compute instances create ${PREFIX}-vm \
    --zone=$ZONE \
    --machine-type=e2-standard-4 \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --network=${PREFIX}-vpc \
    --service-account="${PREFIX}-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --scopes=https://www.googleapis.com/auth/cloud-platform \
    --tags=http-server,https-server \
    --metadata-from-file=startup-script=startup.sh
```

Two fixes from the previous version of this command:

- `--scopes=[https://www.googleapis.com/auth/cloud-platform](https://...)` was a stray Markdown link that had gotten pasted into the command itself — the brackets and second URL would have been passed to `gcloud` as part of the scope value and broken the command. It's a plain URL: `--scopes=https://www.googleapis.com/auth/cloud-platform`.
- `--metadata=startup-script=startup.sh` sets the instance's `startup-script` metadata value to the **literal 8-character string** `startup.sh`, not the contents of the file — GCE would try to execute the text `startup.sh` as a shell script and silently do nothing useful. Reading a script *from* a local file requires `--metadata-from-file=startup-script=startup.sh`, which is what actually installs Docker.

Wait about a minute after creation for `startup.sh` to finish (it installs Docker + the `docker compose` plugin). You can watch it finish with:

```bash
gcloud compute instances get-serial-port-output ${PREFIX}-vm --zone=$ZONE | grep '\[startup\]'
```

## 4. Deploy the application

```bash
gcloud compute ssh ${PREFIX}-vm --zone=$ZONE
```

Inside the VM:

```bash
git clone <your-repository-url> datagen-app
cd datagen-app

cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, GOOGLE_CLOUD_PROJECT (the same $PROJECT_ID
# as above), and Langfuse keys if you're using Langfuse Cloud.
nano .env

sudo docker compose up -d --build
```

(`docker compose`, with a space — the v2 CLI plugin `startup.sh` installs. The standalone hyphenated `docker-compose` v1 binary the original guide used is no longer packaged on current Ubuntu releases.)

Check everything came up healthy:

```bash
sudo docker compose ps
curl http://localhost:8000/healthz
```

Then from your own machine, visit `http://<VM_EXTERNAL_IP>:8501`. Find the external IP with:

```bash
gcloud compute instances describe ${PREFIX}-vm --zone=$ZONE \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

## Troubleshooting

- **`build path ... does not exist`** — you're running `docker compose` from somewhere other than the repo root, or from a checkout where `docker-compose.yml` still lives inside `backend/`. Run it from the directory containing `docker-compose.yml`, `.env`, `backend/`, and `frontend/` as siblings.
- **Backend container restarts in a loop / `GenAI client not initialized`** — check `sudo docker compose logs backend`. Usually either `GOOGLE_CLOUD_PROJECT` isn't set in `.env`, or the VM's service account is missing the `roles/aiplatform.user` binding from step 2.
- **Can't reach the app from your browser** — confirm the firewall rule from step 1 actually applies to this VM (`gcloud compute firewall-rules describe ${PREFIX}-allow-web`) and that you're using the VM's *external* IP, not its internal one.
- **`startup.sh` didn't seem to run** — `--metadata-from-file` (not `--metadata`) is what makes GCE read the script from the local file at instance-creation time; re-check the command in step 3 if you created the VM with the old flag.

## Teardown

Delete resources in reverse order to avoid leaving anything (and its bill) behind:

```bash
gcloud compute instances delete ${PREFIX}-vm --zone=$ZONE --quiet
gcloud compute firewall-rules delete ${PREFIX}-allow-web ${PREFIX}-allow-ssh --quiet
gcloud compute networks delete ${PREFIX}-vpc --quiet
gcloud projects remove-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${PREFIX}-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"
gcloud iam service-accounts delete ${PREFIX}-sa@${PROJECT_ID}.iam.gserviceaccount.com --quiet
```

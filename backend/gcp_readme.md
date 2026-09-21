# GCP Deployment Instructions

This guide walks through deploying the application to Google Cloud Compute Engine using the `gcloud` CLI. All resources utilize the `dbadanin` prefix as requested.

## Prerequisites
Ensure the `gcloud` CLI is installed and authenticated. 

```bash
# 1. Set environment variables
export PROJECT_ID="your-gcp-project-id"
export ZONE="us-central1-a"
export PREFIX="dbadanin"

gcloud config set project $PROJECT_ID


1. Create a Custom Network & Firewall (Optional but Recommended)


gcloud compute networks create ${PREFIX}-vpc --subnet-mode=auto

gcloud compute firewall-rules create ${PREFIX}-allow-web \
    --network=${PREFIX}-vpc \
    --allow=tcp:8501,tcp:8000,tcp:22 \
    --source-ranges=0.0.0.0/0 \
    --description="Allow SSH, Streamlit, and FastAPI"
2. Create a Service Account for Vertex AI

gcloud iam service-accounts create ${PREFIX}-sa \
    --description="Service account for DataGen App" \
    --display-name="${PREFIX}-sa"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${PREFIX}-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"

3. Provision the Virtual Machine
gcloud compute instances create ${PREFIX}-vm \
    --zone=$ZONE \
    --machine-type=e2-standard-4 \
    --network=${PREFIX}-vpc \
    --service-account="${PREFIX}-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --scopes=[https://www.googleapis.com/auth/cloud-platform](https://www.googleapis.com/auth/cloud-platform) \
    --tags=http-server,https-server \
    --metadata=startup-script=startup.sh

4. Deploy Application

# Wait a minute for the VM startup script to finish installing Docker
gcloud compute ssh ${PREFIX}-vm --zone=$ZONE

# Inside the VM:
git clone <your-repository-url> datagen-app
cd datagen-app

# Set your env variables
echo "GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" >> .env
echo "GOOGLE_CLOUD_LOCATION=us-central1" >> .env
# (Add Langfuse keys to .env here if applicable)

sudo docker-compose up -d --build


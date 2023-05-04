#!/usr/bin/env bash

# This is needed for GKE autopilot clusters, circa May 2023

# from: https://cloud.google.com/kubernetes-engine/docs/how-to/workload-identity

NS=sk8s
KSA_NAME=internal-kubectl

GSA_NAME=261248090722-compute@developer.gserviceaccount.com
BUCKET_NAME=jared-genome
PROJECT_ID="jared-genome-analysis"

#kubectl create serviceaccount $KSA_NAME -n $NS

#gcloud iam service-accounts create $GSA_NAME \
#    --project=$PROJECT_ID

gcloud storage buckets add-iam-policy-binding gs://$BUCKET_NAME \
  --member "serviceAccount:$GSA_NAME" \
  --role=roles/storage.objectViewer

gcloud iam service-accounts add-iam-policy-binding $GSA_NAME \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:$PROJECT_ID.svc.id.goog[$NS/$KSA_NAME]"

kubectl annotate serviceaccount $KSA_NAME \
    --namespace $NS \
    261248090722-compute@developer.gserviceaccount.com

#    iam.gke.io/gcp-service-account=$GSA_NAME@$PROJECT_ID.iam.gserviceaccount.com
